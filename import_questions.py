"""把「题库/」里的 txt 试题导入数据库。

【产品逻辑 2026-10-04】
以前题目是 AI 随机出的，现在**所有题目都从教师自己维护的题库里来**。

★ 多小问怎么处理
   一个 txt 里如果有【小问1】【小问2】，**拆成两道独立的题**各自入库，
   但**共享同一份材料**。这样：
     · 两道题都能被抽到，各自独立走完整流程（生成答卷→阅卷→盲评→报告）
     · 不用改 questions / rubric_points 表结构（一道题就是一行）
     · 判分、封顶、抽点、Excel 全部零改动
   例外：原文**没有【答案】**时，采分点无从谈起，会在打印里提醒你补。

用法：
    python import_questions.py                # 导入全部
    python import_questions.py --force        # 已导入的也重导
    python import_questions.py --dry          # 只识别不入库（先看效果）
    python import_questions.py 19_盐风化.txt   # 只处理指定文件

标准格式（每行一个标签，认不出来会转交 AI 兜底）：
    【主题】…
    【满分】12
    【材料】…
    【小问1】
    【设问】…
    【分值】6
    【答案】甲；乙；丙
    【解析】…

设计要点：
    · **原文一个字都不改写**
    · 每点默认 2 分；原文写了分值就按写的
    · 识别结果一定打印出来让你核对 —— 采分点认错会毁掉整道题
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import paths
from core.config import get_api_key, load_config, setup_logging
from core.models import Question, RubricPoint
from core.qbank_parse import parse_file
from services.llm import LLMClient
from services.prompts import load_prompt
from storage import repository as repo

log = logging.getLogger("import_questions")

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
# AI 兜底（标准格式认不出来时才用）
# ---------------------------------------------------------------------------

async def recognize_with_ai(client: LLMClient, text: str) -> Dict[str, Any]:
    """格式不对时调 AI 识别。返回和 parse_file().dicts() 一样的结构。"""
    system = load_prompt("import_question.txt")
    user = (
        "下面是一道高中地理试题的原文，**格式可能不规范**（可能缺标签、"
        "答案用分号分隔等）。请按你收到的要求提取字段，**原文照抄不要改写**。\n\n"
        "==== 试题原文开始 ====\n"
        f"{text}\n"
        "==== 试题原文结束 ====\n\n"
        "只输出 JSON，不要任何其他文字。"
    )
    data = await client.chat_json(system=system, user=user, temperature=0.0)
    if not isinstance(data, dict):
        raise ValueError(f"AI 返回的不是对象：{type(data)}")
    return data


def _ai_dict_to_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 AI 返回的单题 dict 统一成"多个小问"的结构。"""
    pts = []
    for i, p in enumerate(data.get("points") or [], start=1):
        if not isinstance(p, dict):
            continue
        t = str(p.get("text") or "").strip()
        if not t:
            continue
        try:
            sc = float(p.get("score") or DEFAULT_POINT_SCORE)
        except (TypeError, ValueError):
            sc = DEFAULT_POINT_SCORE
        if sc <= 0:
            sc = DEFAULT_POINT_SCORE
        pts.append({"seq": len(pts) + 1, "text": t, "score": sc,
                    "kind": p.get("kind") or "fact"})
    written = data.get("max_score")
    ms = None
    if written is not None:
        try:
            f = float(written)
            if f > 0:
                ms = f
        except (TypeError, ValueError):
            pass
    if ms is None:
        ms = round(sum(p["score"] for p in pts), 2) if pts else 8.0
    return [{
        "ok": bool(data.get("stem") and pts),
        "sub_no": 1,
        "topic": data.get("topic") or "题库导入",
        "stem": str(data.get("stem") or "").strip(),
        "material": str(data.get("material") or "").strip(),
        "has_answer": bool(pts),
        "max_score": ms,
        "max_score_source": ("written" if written
                             else ("inferred" if pts else "fallback")),
        "points": pts,
        "analysis": str(data.get("analysis") or "").strip(),
    }]


# ---------------------------------------------------------------------------
# 打印核对
# ---------------------------------------------------------------------------

def print_check(name: str, pf, rows: List[Dict[str, Any]]) -> None:
    """把识别结果打出来，让教师核对。采分点认错 = 整道题废，所以必须看。"""
    line = "═" * 70
    print()
    print(f"┌{line}")
    print(f"│ 文件：{name}")
    if pf.topic:
        print(f"│ 主题：{pf.topic}")
    print(f"│ 材料：{len(pf.material)} 字（下面每道题共用这一份）")
    print(f"│ ★ 拆成 {len(rows)} 道独立的题：")
    for r in rows:
        n = len(r["points"])
        src = ("原文写的" if r["max_score_source"] == "written"
               else "按采分点数×2 推断" if r["max_score_source"] == "inferred"
               else "暂定，请核对")
        print(f"│")
        print(f"│   【小问{r['sub_no']}】满分 {r['max_score']:g} 分（{src}）"
              f"　采分点 {n} 个")
        print(f"│     设问：{r['stem']}")
        for p in r["points"]:
            print(f"│     {p['seq']}. {p['text']}  —— {p['score']:g} 分")
        # 【评分说明】= 教师写的判分硬约束，必须让你在导入时就看到它有没有被读到
        if r.get("note"):
            print(f"│     ★ 评分说明：{r['note']}")
        if not n:
            print(f"│     ⚠ 没有采分点！原文里没有【答案{r['sub_no']}】吗？")
    if pf.analysis:
        print(f"│ 解析：{pf.analysis[:70]}{'…' if len(pf.analysis) > 70 else ''}")
    for w in pf.warnings:
        print(f"│ ⚠ {w}")
    print(f"└{line}")


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------

def _drop_bank_file_row(file_id: int) -> bool:
    """删掉 qbank_files 里的一行（文件被改名后重导时用）。

    qbank_files.filename 是 UNIQUE 的：清完题目如果不删这一行，
    下次再判重还会撞上这个旧文件名。返回是否真的删了。
    """
    from storage.db import get_conn

    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM qbank_files WHERE id=?",
                           (int(file_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_rows(name: str, raw_text: str, rows: List[Dict[str, Any]],
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def run(files: List[Path], force: bool, dry: bool) -> int:
    cfg = load_config()
    key = get_api_key(cfg)

    if not files:
        print(f"题库目录是空的：{BANK_DIR}")
        print("把试题的 .txt 放进去，再运行一次。")
        return 0

    # 判重。两层：
    #   ① 文件名一样 → 已导入过（这是最常见的情况）
    #   ② **内容 sha1 一样但文件名不同** → 教师改名了。
    #      这时必须提醒：不然同一道题会在题库里出现两份（用户实测踩过：
    #      19_xxx.txt 改成 01_xxx.txt，导入后题库变成 4 道题、2 道重复）。
    todo: List[Path] = []
    for p in files:
        got = repo.get_file_imported(p.name)
        if got and not force:
            print(f"· 跳过（已导入过）：{p.name}  → {got[1]} 道题")
            continue

        if not got:
            try:
                sha = sha1_of(read_text(p))
            except OSError:
                sha = ""
            same = repo.find_file_by_sha1(sha) if sha else None
            if same and same["filename"] != p.name and not force:
                print()
                print(f"⚠ 注意到「{p.name}」的内容和已导入的"
                      f"「{same['filename']}」完全一样")
                print(f"   （只是文件名改了）。如果直接导入，题库里会出现"
                      f"两份同样的题。")
                print(f"   想替换掉旧的，就加 --force 重跑：")
                print(f"       python import_questions.py --force")
                print()
                continue
        todo.append(p)

    if not todo:
        print("\n没有需要导入的新文件。要重导的话加 --force")
        return 0

    print()
    print("=" * 70)
    print(f"  准备处理 {len(todo)} 个文件：")
    for p in todo:
        print(f"    · {p.name}")
    print("=" * 70)

    # 只有真正需要 AI 兜底时才建客户端（没配密钥也能跑纯本地的情况）
    client: Optional[LLMClient] = None
    ok_n = 0
    total_q = 0
    need_review: List[str] = []
    ai_used = 0

    try:
        for i, path in enumerate(todo, start=1):
            name = path.name
            text = read_text(path)
            print()
            print(f"[{i}/{len(todo)}] {name}（{len(text)} 字）")

            pf = parse_file(text)
            rows = pf.dicts()
            src = "written"
            analysis = pf.analysis
            topic = pf.topic

            # 标准格式认不全 → 转交 AI
            if not pf.ok:
                print("    · 标准格式没认全，转交 AI 识别…")
                if not key:
                    print(f"    ✗ 认不出格式，又没配 API 密钥，无法自动处理。")
                    print(f"      请检查 {name} 的格式，或在 .env 里配好密钥后重跑。")
                    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, REVIEW_DIR / name)
                    need_review.append(name)
                    continue
                if client is None:
                    client = LLMClient(cfg, key)
                try:
                    data = await recognize_with_ai(client, text)
                    rows = _ai_dict_to_rows(data)
                    src = "ai"
                    ai_used += 1
                    # AI 识别不出答案时，把本地读到的主题/解析也带上
                    for r in rows:
                        if not r.get("topic") or r["topic"] == "题库导入":
                            r["topic"] = topic or "题库导入"
                        if not r.get("analysis"):
                            r["analysis"] = analysis
                except Exception as e:  # noqa: BLE001
                    log.exception("AI 识别失败")
                    print(f"    ✗ AI 识别失败：{e}")
                    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, REVIEW_DIR / name)
                    need_review.append(name)
                    continue

            if not rows:
                print("    ✗ 一道题都没提取出来")
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, REVIEW_DIR / name)
                need_review.append(name)
                continue

            warnings = list(pf.warnings)
            print_check(name, pf, rows)

            if dry:
                print("    （--dry 模式，不入库）")
                continue

            # 重导：先清掉旧版本。
            # 两种情况都要清：
            #   ① 同名 → clear_bank_questions(force=True)，连练习记录一起清
            #   ② 改名了但内容一样 → 把**旧文件名**那份也清掉，
            #      否则题库里会留着一份没人要的重复题。
            #      （save_bank_file 靠 filename 唯一，所以还得删掉旧的
            #        qbank_files 行，否则下次判重还会撞上它）
            got = repo.get_file_imported(name)
            if got and force:
                r = repo.clear_bank_questions(got[0], force=True)
                if r["deleted"]:
                    print(f"    · 清掉同名旧版：题目 id {r['deleted']}")

            old_same = repo.find_file_by_sha1(sha1_of(text))
            if old_same and old_same["filename"] != name:
                r = repo.clear_bank_questions(old_same["id"], force=True)
                if r["deleted"] or r["kept"]:
                    print(f"    · 清掉改名前的旧文件「{old_same['filename']}」："
                          f"题目 id {r['deleted'] or r['kept']}")
                conn_del = _drop_bank_file_row(old_same["id"])
                if conn_del:
                    print(f"      （同时移除了它的导入记录）")

            qids = save_rows(name, text, rows, topic, analysis,
                             pf.has_answer, src, warnings)
            total_q += len(qids)
            print(f"    ✓ 已入库：{len(qids)} 道题，题目 id = {qids}")
            ok_n += 1

            if warnings:
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, REVIEW_DIR / name)
                need_review.append(name)
    finally:
        if client is not None:
            await client.aclose()

    print()
    print("=" * 70)
    print(f"  完成：{ok_n} 个文件，拆出 {total_q} 道独立的题")
    if ai_used:
        print(f"        （其中 {ai_used} 个文件用 AI 兜底识别）")
    if need_review:
        print()
        print(f"  ⚠ 需要你核对 {len(need_review)} 个文件（副本在 {REVIEW_DIR}）：")
        for n in need_review:
            print(f"    - {n}")
    print("=" * 70)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="把题库里的 txt 导入数据库")
    ap.add_argument("files", nargs="*", help="只处理指定的 txt")
    ap.add_argument("--force", action="store_true", help="已导入的也重导")
    ap.add_argument("--dry", action="store_true", help="只识别不入库")
    args = ap.parse_args()

    setup_logging()
    BANK_DIR.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [Path(f) if Path(f).is_absolute() else BANK_DIR / f
                 for f in args.files]
        missing = [f for f in files if not f.exists()]
        if missing:
            print("找不到：", [str(m) for m in missing])
            return 2
    else:
        files = list_txt_files()

    print()
    print("=" * 70)
    print("  题库导入")
    print("=" * 70)
    print(f"  题库目录：{BANK_DIR}")
    print(f"  待处理：{len(files)} 个文件")
    print()

    return asyncio.run(run(files, args.force, args.dry))


if __name__ == "__main__":
    sys.exit(main())
