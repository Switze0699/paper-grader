"""题库导入的核心逻辑（本地解析 + 入库）。

【为什么要有这个文件2026-10-05】
教师往「题库/」文件夹里丢了一个新 txt，批改器里却看不到新题——
因为批改器读的是数据库，**不会自动扫目录**。以前只能让我命令行跑
`import_questions.py`，老师不会用命令行。

所以抽出一个**能被界面直接调用**的 `refresh()`：
  · 纯本地解析，不花 API 额度（老师很在意这个）
  · 不 print，返回结构化结果给界面显示
  · 与命令行版**共用同一套**解析/入库代码（避免两套逻辑跑偏）

`import_questions.py`（命令行版）也改成调用这里的函数，
它多出来的能力只有一个：格式认不全时转交 AI 兜底。

标准格式（每行一个标签，认不出来会转命令行版走 AI）：
    【主题】…
    【满分】12
    【材料】…
    【小问1】
    【设问】…
    【分值】6
    【答案】甲；乙；丙
    【评分说明】…（可选，封顶规则）
    【解析】…
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from core import paths
from core.models import Question, RubricPoint
from core.qbank_parse import parse_file
from storage import repository as repo
from storage.db import get_conn

log = logging.getLogger("qbank_import")

BANK_DIR = paths.user_root() / "题库"
REVIEW_DIR = BANK_DIR / "_待确认"

# txt 的编码候选：老师可能存成 GBK（Windows 默认）
ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5")

# 每点默认分值
DEFAULT_POINT_SCORE = 2.0


# ---------------------------------------------------------------------------
# 读文件
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def list_txt_files() -> List[Path]:
    """列出题库里所有 txt（跳过 _开头的目录，如 _待确认）。"""
    if not BANK_DIR.exists():
        return []
    out: List[Path] = []
    for p in sorted(BANK_DIR.rglob("*.txt")):
        rel = p.relative_to(BANK_DIR)
        if any(part.startswith("_") for part in rel.parts[:-1]):
            continue
        if p.name.startswith("~$"):
            continue          # Office 临时文件
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 结果结构（给界面显示用）
# ---------------------------------------------------------------------------

@dataclass
class SubBrief:
    """一道题（一个小问）的摘要，给界面核对用。"""
    sub_no: int
    max_score: float
    n_points: int
    stem: str
    note: str = ""
    max_score_source: str = "written"


@dataclass
class FileResult:
    """一个 txt 的处理结果。"""
    name: str
    status: str                    # imported / skipped / failed
    message: str = ""
    subs: List[SubBrief] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    qids: List[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "imported"


@dataclass
class RefreshResult:
    """一次刷新的总结果。"""
    files: List[FileResult] = field(default_factory=list)
    bank_dir: Path = BANK_DIR

    @property
    def imported(self) -> List[FileResult]:
        return [f for f in self.files if f.status == "imported"]

    @property
    def skipped(self) -> List[FileResult]:
        return [f for f in self.files if f.status == "skipped"]

    @property
    def failed(self) -> List[FileResult]:
        return [f for f in self.files if f.status == "failed"]

    @property
    def added_questions(self) -> int:
        return sum(len(f.qids) for f in self.imported)

    @property
    def has_problem(self) -> bool:
        return bool(self.failed)


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------

def _drop_bank_file_row(file_id: int) -> bool:
    """删掉 qbank_files 里的一行（文件被改名后重导时用）。

    qbank_files.filename 是 UNIQUE 的：清完题目如果不删这一行，
    下次再判重还会撞上这个旧文件名。返回是否真的删了。
    """
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM qbank_files WHERE id=?", (int(file_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_rows(name: str, raw_text: str, rows: List[dict],
              topic: str, analysis: str, has_answer: bool,
              source: str, warnings: List[str]) -> List[int]:
    """把 N 道题入库，返回它们的 qid。

    顺序很重要：先 save_bank_file（拿到 file_id），
    再逐个小问 save_question + link_bank_question。
    """
    file_id = repo.save_bank_file(
        filename=name, raw_text=raw_text, sha1=sha1_of(raw_text),
        parse_status="need_review" if warnings else "ok",
        n_subs=len(rows), analysis=analysis, has_answer=has_answer,
        max_score_source=source, warnings=" | ".join(warnings))

    qids: List[int] = []
    for r in rows:
        q = Question(
            subject="地理",
            topic=r.get("topic") or topic or "题库导入",
            stem=r["stem"],
            max_score=float(r["max_score"]),
            points=[RubricPoint(seq=p["seq"], text=p["text"],
                                score=float(p["score"]))
                    for p in r["points"]],
        )
        qid = repo.save_question(q)
        repo.link_bank_question(file_id, int(r["sub_no"]), qid,
                                sub_stem=r["stem"],
                                sub_score=float(r["max_score"]),
                                sub_note=r.get("note", ""))
        qids.append(qid)
    return qids


def _briefs(rows: List[dict]) -> List[SubBrief]:
    return [
        SubBrief(sub_no=int(r["sub_no"]),
                 max_score=float(r["max_score"]),
                 n_points=len(r["points"]),
                 stem=r["stem"],
                 note=r.get("note", ""),
                 max_score_source=r.get("max_score_source", "written"))
        for r in rows
    ]


def review_dir() -> Path:
    """_待确认 目录（跟着 BANK_DIR 走）。

    ⚠ 不要用模块级常量：测试会把BANK_DIR 换成临时目录，
      跟着算才不会把测试副本写进你真实的题库文件夹。
    """
    return BANK_DIR / "_待确认"


def _to_review(name: str, src: Optional[Path]) -> None:
    """格式认不出来时，把副本留在 _待确认 让教师核对。"""
    dst_dir = review_dir()
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src is not None and src.exists():
            shutil.copy2(src, dst_dir / name)
        else:
            (dst_dir / name).write_text("", encoding="utf-8")
    except OSError:
        log.exception("复制到 _待确认 失败：%s", name)


# ---------------------------------------------------------------------------
# ★ 主入口：刷新题库
# ---------------------------------------------------------------------------

def refresh(force: bool = False,
            only: Optional[List[str]] = None) -> RefreshResult:
    """扫描题库目录，把没导过的 txt 导入数据库。

    ⚠ **纯本地解析，不调 AI**（不花 API 额度）。格式认不出来的文件
      会被标成 failed 并在 _待确认 留副本，教师可以命令行跑
      `import_questions.py` 走 AI 兜底。

    force=True：已导入过的也重导（会删掉旧题重建，**先备份数据库**）。
    only：只处理指定文件名（调试用）。
    """
    res = RefreshResult()
    try:
        files = ([BANK_DIR / n for n in only] if only else list_txt_files())
    except OSError:
        log.exception("扫题库目录失败")
        res.files.append(FileResult(
            name="(题库目录)", status="failed",
            message=f"读不到题库文件夹：{BANK_DIR}"))
        return res

    for path in files:
        if not path.exists():
            res.files.append(FileResult(
                name=path.name, status="failed", message="文件不存在"))
            continue
        r = _import_one(path, force)
        res.files.append(r)

    return res


def _import_one(path: Path, force: bool) -> FileResult:
    name = path.name

    # ---- 判重三层（顺序有讲究）----
    got = repo.get_file_imported(name)
    if got and not force:
        return FileResult(
            name=name, status="skipped",
            message=f"已经导入过了（{got[1]} 道题）")

    try:
        text = read_text(path)
    except OSError as e:
        return FileResult(name=name, status="failed", message=f"读不了文件：{e}")

    sha = sha1_of(text)
    if not got:
        # ② 内容一样但文件名不同 → 教师只是改了个名。
        #    直接导会在题库里出现两份一样的题，必须拦下来让他选。
        same = repo.find_file_by_sha1(sha)
        if same and same["filename"] != name and not force:
            return FileResult(
                name=name, status="failed",
                message=(f"内容和已导入的「{same['filename']}」完全一样"
                         f"（只是改了文件名）。\n"
                         f"要替换掉旧的那份，请用「重新导入全部」。"))

    # ---- 解析 ----
    try:
        pf = parse_file(text)
    except Exception as e:  # noqa: BLE001
        log.exception("解析失败：%s", name)
        _to_review(name, path)
        return FileResult(name=name, status="failed",
                          message=f"解析出错：{e}")

    rows = pf.dicts()
    if not rows:
        _to_review(name, path)
        why = "没读到【材料】或【小问】"if not pf.subs else "没读到【答案】，采分点为空"
        return FileResult(
            name=name, status="failed",
            message=(f"{why}。\n副本已放到「{review_dir().name}」文件夹，"
                     f"可以命令行跑 import_questions.py 让 AI 帮你认。"),
            warnings=list(pf.warnings))

    if not pf.ok:
        # 认不全，但还能读出题来→ 先入库 + 提醒，别卡住老师
        log.warning("格式没认全但仍入库：%s（%s）", name, pf.warnings)

    # ---- 重导前的清理 ----
    if got and force:
        cleared = repo.clear_bank_questions(got[0], force=True)
        if cleared["deleted"]:
            log.info("清掉同名旧版 %s：%s", name, cleared["deleted"])

    old_same = repo.find_file_by_sha1(sha)
    if old_same and old_same["filename"] != name:
        cleared = repo.clear_bank_questions(old_same["id"], force=True)
        if cleared["deleted"] or cleared["kept"]:
            log.info("清掉改名前的旧文件 %s：%s",
                     old_same["filename"], cleared["deleted"] or cleared["kept"])
        _drop_bank_file_row(old_same["id"])

    # ---- 入库 ----
    try:
        qids = save_rows(name, text, rows, pf.topic, pf.analysis,
                         pf.has_answer, "written", list(pf.warnings))
    except Exception as e:  # noqa: BLE001
        log.exception("入库失败：%s", name)
        _to_review(name, path)
        return FileResult(name=name, status="failed", message=f"入库失败：{e}")

    if pf.warnings:
        _to_review(name, path)

    return FileResult(
        name=name, status="imported",
        message=f"新增 {len(qids)} 道题",
        subs=_briefs(rows), warnings=list(pf.warnings), qids=qids)
