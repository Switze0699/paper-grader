"""把「题库/」里的 txt 试题导入数据库。

【产品逻辑 2026-10-04 起】
以前题目是 AI 随机出的，现在**所有题目都从教师自己维护的题库里来**。
这个脚本负责把 txt（格式不固定）识别成结构化的题目 + 采分点。

用法（双击或命令行都行）：
    python import_questions.py                # 导入全部
    python import_questions.py --force        # 已导入的也重导（会覆盖）
    python import_questions.py --dry          # 只识别不入库（先看效果）
    python import_questions.py 某题.txt       # 只处理指定的文件

设计要点：
- **原文一个字都不改。** AI 只做"分段"和"切分点"，不做润色。
- 每点默认 2 分；原文写了分值就按写的（99% 的题都是 2 分）。
- 满分：原文写了按写的；没写就按"采分点 × 2"推断。
- 多余的采分点作��"候补"，答对也不加分（复用已有的 cap_score 封顶）。
- **识别结果一定打印出来**，让教师核对 —— 采分点认错会毁掉整道题。
- 识别不确定的，文件会被复制到 题库/_待确认/，等人看一眼。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import paths
from core.config import get_api_key, load_config, setup_logging
from core.models import Question, RubricPoint
from services.llm import LLMClient
from services.prompts import load_prompt
from storage import repository as repo

log = logging.getLogger("import_questions")

BANK_DIR = paths.user_root() / "题库"
REVIEW_DIR = BANK_DIR / "_待确认"

# 跳过这些目录（不是题目文件）
SKIP_DIR_PREFIX = "_"

# 每点的默认分值
DEFAULT_POINT_SCORE = 2.0

# txt 的编码候选：老师可能存成 GBK（Windows 默认）
ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5")


# ---------------------------------------------------------------------------
# 读文件
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """读 txt，自动试几种编码。"""
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # 都失败就按 utf-8 硬解，替换掉坏字符（宁可少字也别中断）
    return raw.decode("utf-8", errors="replace")


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def list_txt_files() -> List[Path]:
    """列出题库里所有 txt（跳过 _开头的目录）。"""
    if not BANK_DIR.exists():
        return []
    out: List[Path] = []
    for p in sorted(BANK_DIR.rglob("*.txt")):
        rel = p.relative_to(BANK_DIR)
        if any(part.startswith(SKIP_DIR_PREFIX) for part in rel.parts[:-1]):
            continue          # _待确认/ 这类目录里的不算
        if p.name.startswith("~$"):
            continue          # Office 临时文件
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 本地规则：先自己猜，猜得准就不用花 AI 调用
# ---------------------------------------------------------------------------

# 设问通常以这些词开头/结尾
_STEM_HINT = re.compile(
    r"(分析|说明|指出|阐述|简述|描述|比较|判断|评价|解释|探讨|概述|归纳|"
    r"试述|论述|分析并|简析|说明并|指出并)")

# 明显的分值标记
_SCORE_PATTERNS = [
    re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]"),
    re.compile(r"满分\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分"),
    re.compile(r"本题\s*(\d+(?:\.\d+)?)\s*分"),
]

# 段落标签（材料/答案的各种叫法）
_ANSWER_LABELS = [
    "答案", "参考答案", "评分标准", "得分点", "采分点", "评分要点",
    "答案要点", "参考答案与评分标准", "要点", "评分细则",
]
_MATERIAL_LABELS = ["材料", "材料一", "材料二", "材料三", "题干材料", "阅读材料"]


def guess_split(text: str) -> Dict[str, Optional[str]]:
    """用规则猜哪段是设问/材料/答案。

    只在"有明确标签"时才给答案（材料/答案：），其他情况返回空，
    交给 AI 处理 —— 宁可多花一次调用，也不要猜错把材料当答案。
    """
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    # 找到"答案"标签第一次出现的位置
    ans_idx = None
    for i, l in enumerate(lines):
        s = l.strip()
        for lab in _ANSWER_LABELS:
            if s.startswith(lab) and (len(s) == len(lab) or s[len(lab)] in "：:（("):
                ans_idx = i
                break
        if ans_idx is not None:
            break
    if ans_idx is None:
        return {"stem": None, "material": None, "answer": None}

    answer = "\n".join(lines[ans_idx + 1:]).strip()
    head = "\n".join(lines[:ans_idx]).strip()

    # 头部里找设问（取含"分析/说明"等词的最后一行）
    head_lines = [l.strip() for l in head.split("\n") if l.strip()]
    stem = None
    for l in head_lines:
        if _STEM_HINT.search(l):
            stem = l
    # 设问也可能在标签"题目：/设问："后面
    for i, l in enumerate(head_lines):
        if re.match(r"^(题目|设问|问题)\s*[:：]", l):
            stem = l.split(":", 1)[-1].split("：", 1)[-1].strip() or l
            # 去掉紧跟的标签行
            if i + 1 < len(head_lines) and _STEM_HINT.search(head_lines[i + 1]):
                stem = head_lines[i + 1]
    if stem is None and head_lines:
        stem = head_lines[0]

    # 去掉设问那行，剩下的当材料
    mat_lines = [l for l in head_lines if l != stem]
    material = "\n".join(mat_lines).strip()

    return {"stem": stem, "material": material or None, "answer": answer or None}


def guess_max_score(text: str) -> Optional[float]:
    """从原文里找明确写的分值。"""
    # 只看设问附近和标题行，避免把材料里的"3分"误当成满分
    for line in text.split("\n")[:6]:
        for pat in _SCORE_PATTERNS:
            m = pat.search(line)
            if m:
                return float(m.group(1))
    return None


def count_numbered_points(answer: str) -> int:
    """数一下答案里有几个采分点（1. / ① / - 三种都认）。"""
    if not answer:
        return 0
    n = 0
    for line in answer.split("\n"):
        s = line.strip()
        if re.match(r"^\d+[.、．]", s):
            n += 1
        elif re.match(r"^[①-⑳]", s):
            n += 1
        elif re.match(r"^[-—•·]\s*\S", s):
            n += 1
    return n


def needs_ai(text: str) -> bool:
    """判断本地规则够不够用。

    规则能搞定的：有明确"答案："标签 + 答案里有编号行。
    搞不定的（要调 AI）：没标签、答案里没编号、设问找不到。

    """
    g = guess_split(text)
    if g["answer"] is not None and count_numbered_points(g["answer"]) > 0 \
            and g["stem"] is not None:
        return False        # 本地能完整搞定

    # 到这里说明本地没把握。判断是"本来就没答案"还是"有答案但没标签"：
    #   文本够长 + 已经有明显设问句 → 多半是答案混在正文里（没标签），交给 AI
    #   文本很短 / 没有任何设问特征 → 大概真没答案，让 AI 确认一下即可
    #     （这种也走 AI：万一有"参考解析"之类的小标题没被认出来呢）
    return True


def _looks_like_has_answer(text: str) -> bool:
    """扫一遍有没有任何"答案类"标签。"""
    for line in text.split("\n"):
        s = line.strip()
        for lab in _ANSWER_LABELS:
            if s.startswith(lab) and (len(s) == len(lab) or s[len(lab)] in "：:（("):
                return True
    return False


# ---------------------------------------------------------------------------
# AI 识别
# ---------------------------------------------------------------------------

async def recognize(client: LLMClient, text: str) -> Dict[str, Any]:
    """调 AI 做结构化提取。提示词里写死了"不许改写"。"""
    system = load_prompt("import_question.txt")
    user = (
        "下面是一道高中地理试题的原文，格式可能不规范。"
        "请按你收到的要求提取字段，**原文照抄不要改写**。\n\n"
        "==== 试题原文开始 ====\n"
        f"{text}\n"
        "==== 试题原文结束 ====\n\n"
        "只输出 JSON，不要任何其他文字。"
    )
    data = await client.chat_json(system=system, user=user, temperature=0.0)
    if not isinstance(data, dict):
        raise ValueError(f"AI 返回的不是对象：{type(data)}")
    return data


def normalize(data: Dict[str, Any], filename: str) -> tuple:
    """把 AI 返回的 dict 转成 (Question, 各种标记)。

    分值规则全在这里落地：
    - 每点默认 2 分
    - 满分：原文写了用写的；没写按 点数 × 2
    """
    warnings: List[str] = []

    stem = (data.get("stem") or "").strip()
    material = (data.get("material") or "").strip()
    analysis = (data.get("analysis") or "").strip()
    has_answer = bool(data.get("has_answer", True))

    if not stem:
        warnings.append("没能识别出设问")

    # ---- 采分点 ----
    points: List[RubricPoint] = []
    raw_points = data.get("points") or []
    for i, p in enumerate(raw_points, start=1):
        if not isinstance(p, dict):
            continue
        txt = (p.get("text") or "").strip()
        if not txt:
            continue
        # 分值：AI 读到的就信它；读不到一律 2 分
        sc = p.get("score")
        try:
            sc = float(sc) if sc is not None else DEFAULT_POINT_SCORE
        except (TypeError, ValueError):
            sc = DEFAULT_POINT_SCORE
        if sc <= 0:
            sc = DEFAULT_POINT_SCORE
        points.append(RubricPoint(seq=i, text=txt, score=sc))

    # 本地兜底：AI 没给点，但本地规则数得出有几个 → 提醒而不是瞎填
    if not points and has_answer:
        warnings.append("AI 没给出采分点（如果原文确实有答案，请手工补）")

    # ---- 满分 ----
    # 规则（用户定的）：
    #   1. 原文写了分值 → 用写的
    #   2. 没写 → 按「各采分点分值之和」推断（每点默认 2 分）
    #   3. 连采分点都没有（原文没答案）→ 只能暂定，请人工核对
    written = data.get("max_score")
    source = (data.get("max_score_source") or "").strip()
    max_score: Optional[float] = None
    if written is not None:
        try:
            f = float(written)
            if f > 0:
                max_score = f
                source = "written"
        except (TypeError, ValueError):
            pass
    if max_score is None and points:
        # 推断：把各点分值加起来（没标分值的点已经按 2 分填了）
        max_score = round(sum(p.score for p in points), 2)
        if source != "written":
            source = "inferred"
    if max_score is None:
        # 原文没答案也写分值 → 定不出来，暂定 8 分让人来改
        max_score = 8.0
        source = "fallback"
        warnings.append("原文里既没有答案也没写分值，满分暂按 8 分，请你手工核对")

    # AI 说不确定的
    unclear = data.get("unclear_parts") or []
    if isinstance(unclear, list) and unclear:
        warnings.append("AI 标注了不确定的地方：" + "；".join(str(u) for u in unclear))
    if not data.get("ok", True):
        warnings.append("AI 自己说提取不完整")

    # ---- 候补提示（只是提醒，不改数据）----
    need = 0
    acc = 0.0
    for p in points:
        if acc >= max_score:
            break
        acc += p.score
        need += 1
    if points and need < len(points):
        warnings.append(
            f"共 {len(points)} 个采分点，但 {need} 个就到满分 {max_score:g} 分了，"
            f"多出的 {len(points) - need} 个作候补（答对也不加分）"
        )

    q = Question(
        subject="地理",
        topic=(data.get("topic") or "题库导入").strip() or "题库导入",
        stem=stem,
        max_score=max_score,
        points=points,
    )
    return q, analysis, has_answer, source, warnings


# ---------------------------------------------------------------------------
# 打印核对
# ---------------------------------------------------------------------------

def print_check(name: str, q: Question, analysis: str, has_answer: bool,
                source: str, warnings: List[str], raw_text: str) -> None:
    line = "─" * 66
    print()
    print(f"┌{line}")
    print(f"│ 文件：{name}")
    print(f"│ 【设问】{q.stem or '（没识别出来）'}")
    mat = (q.stem and getattr(q, '_mat', '')) or ""
    print(f"│ 【材料】{('（' + str(len(raw_text)) + ' 字，见文件）') if mat == '' else mat}")
    print(f"│ 【满分】{q.max_score:g} 分"
          + ("（原文写的）" if source == "written" else
             "（按采分点数 × 2 推断）" if source == "inferred" else "（暂定，请核对）"))
    print(f"│ 【答案】{'有' if has_answer else '⚠ 原文里没有答案'}")
    if q.points:
        print(f"│ 【采分点】共 {len(q.points)} 个")
        for p in q.points:
            print(f"│    {p.seq}. {p.text}    —— {p.score:g} 分")
    else:
        print("│ 【采分点】⚠ 没有！")
    if analysis:
        print(f"│ 【解析】{analysis}")
    for w in warnings:
        print(f"│ ⚠ {w}")
    print(f"└{line}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def already_imported(filename: str) -> Optional[int]:
    qid = repo.get_file_imported_qid(filename)
    return qid


async def run(files: List[Path], force: bool, dry: bool) -> int:
    cfg = load_config()
    key = get_api_key(cfg)
    if not key:
        print("✗ 没有读到 API 密钥，请先在 .env 里配置 ZHIPU_API_KEY")
        return 2

    if not files:
        print(f"题库目录是空的：{BANK_DIR}")
        print("把试题的 .txt 放进去，再运行一次。")
        return 0

    todo: List[Path] = []
    for p in files:
        if not force and already_imported(p.name):
            print(f"· 跳过（已导入过）：{p.name}")
            continue
        todo.append(p)

    if not todo:
        print("\n没有需要导入的新文件。")
        print("要重导的话加 --force。")
        return 0

    print(f"准备处理 {len(todo)} 个文件：")
    for p in todo:
        print(f"  · {p.name}")
    print()

    client = LLMClient(cfg, key)
    ok_n = 0
    need_review: List[str] = []
    try:
        for i, path in enumerate(todo, start=1):
            name = path.name
            text = read_text(path)
            print()
            print(f"[{i}/{len(todo)}] {name} （{len(text)} 字）")

            # 本地规则先试
            local = guess_split(text)
            use_ai = needs_ai(text)
            print(f"    本地识别：{'能搞定' if not use_ai else '搞不定，要调 AI'}")

            try:
                if use_ai:
                    data = await recognize(client, text)
                else:
                    # 本地就能搞定：不花 AI 调用
                    data = _from_local(local, text)
                q, analysis, has_ans, source, warns = normalize(data, name)
                # 把材料塞进去（只用于打印，Question 本身不存材料）
                q._mat = local.get("material") or data.get("material") or ""
            except Exception as e:  # noqa: BLE001
                log.exception("识别失败")
                print(f"    ✗ 识别失败：{e}")
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, REVIEW_DIR / name)
                need_review.append(name)
                print(f"    → 原文件已复制到 {REVIEW_DIR.name}/ 供你手工处理")
                continue

            # 打印核对
            print_check(name, q, analysis, has_ans, source, warns, text)

            if dry:
                print("    （--dry 模式，不入库）")
                continue

            # 入库
            if warns:
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, REVIEW_DIR / name)
                need_review.append(name)

            qid = save_one(q, name, text, analysis, has_ans, source, warns)
            print(f"    ✓ 已入库（题目 id={qid}）")
            ok_n += 1
    finally:
        await client.aclose()

    print()
    print("=" * 68)
    print(f"  完成：成功 {ok_n} 个")
    if need_review:
        print(f"  需要你核对：{len(need_review)} 个")
        for n in need_review:
            print(f"    - {n}")
        print(f"  这些文件的副本在：{REVIEW_DIR}")
        print("  请打开核对采分点和分值，改好 txt 后重跑本脚本（加 --force）")
    if not dry:
        print()
        print("  接下来：启动程序 → 首页点「开始练习」")
    print("=" * 68)
    return 0


def _from_local(local: Dict[str, Optional[str]], text: str) -> Dict[str, Any]:
    """本地规则的结果，转成和 AI 一样的 dict 结构（省 AI 调用）。"""
    answer = local.get("answer") or ""
    points = []
    for line in answer.split("\n"):
        s = line.strip()
        m = re.match(r"^(?:\d+[.、．]|[-—•·])\s*(.+)$", s)
        if m:
            txt = m.group(1).strip()
        elif re.match(r"^[①-⑳]\s*(.+)$", s):
            txt = re.sub(r"^[①-⑳]\s*", "", s).strip()
        else:
            continue
        # 去掉尾部 "(2分)" 之类，但记下分值
        sc = DEFAULT_POINT_SCORE
        m2 = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*$", txt)
        if m2:
            sc = float(m2.group(1))
            txt = txt[:m2.start()].strip()
        points.append({"seq": len(points) + 1, "text": txt, "score": sc})

    written = guess_max_score(text)
    return {
        "ok": True,
        "stem": local.get("stem") or "",
        "material": local.get("material") or "",
        "has_answer": bool(answer),
        "max_score": written,
        "max_score_source": "written" if written else "inferred",
        "points": points,
        "analysis": "",
        "unclear_parts": [],
    }


def save_one(q: Question, filename: str, raw_text: str, analysis: str,
             has_answer: bool, source: str, warnings: List[str]) -> int:
    """存进数据库：questions + rubric_points + qbank_files。

    ⚠ 走一个新的 qbank_files 表记"这个 txt 已经导过了"，
    不往 questions 表加字段 —— 这样万一这套逻辑有问题，
    删掉新表就回到现在的状态，你的 42 份存档不受影响。
    """
    qid = repo.save_question(q)
    repo.save_bank_file(filename, raw_text, sha1_of(raw_text),
                        "need_review" if warnings else "ok", qid,
                        analysis=analysis, has_answer=has_answer,
                        max_score_source=source,
                        warnings=" | ".join(warnings))
    return qid


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="把题库里的 txt 导入数据库")
    ap.add_argument("files", nargs="*", help="只处理指定的 txt（可多个）")
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
    print("=" * 68)
    print("  题库导入")
    print("=" * 68)
    print(f"  题库目录：{BANK_DIR}")
    print(f"  待处理：{len(files)} 个文件")
    print()

    return asyncio.run(run(files, args.force, args.dry))


if __name__ == "__main__":
    sys.exit(main())
