"""从一段试题原文里，识别出「一道或多道」题目。

【为什么要拆成"多个 Question"】
2026-10-04 用户决定：多小问的大题**拆成多道独立题**，每道题都带同一段材料。
好处是能直接复用现成的 questions / rubric_points 表（一题一行、一个设问一组点），
判分、封顶、抽点、Excel 全部一行不改；而且两个小问的分数是分开的，
一眼能看出学生"哪一问强、哪一问弱"。

本模块只做"读文本 → 结构化"，不管入库（那是 import_questions.py 的事）。

⚠ 三条铁律（写在提示词里，也在这里强制）：
    1. 原文一个字都不改写
    2. 每点默认 2 分，原文写了才按写的
    3. 满分：原文写了按写的，没写按"采分点数 × 2"
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

DEFAULT_POINT_SCORE = 2.0

# ---------------------------------------------------------------------------
# 标签与模式
# ---------------------------------------------------------------------------

# 小问标题：支持 小问1（6分）： / （1）分析… / ①分析… / 1. 分析…
_SUB_HEADS = [
    # 小问1（6分）：xxx  /  第1问（6分）：xxx
    re.compile(r"^\s*(?:小问|第)\s*([0-9一二三四五六七八九十]+)\s*"
               r"[（(]?\s*(\d+(?:\.\d+)?)\s*分?\s*[）)]?\s*[：:．.、]\s*"
               r"(.+)$"),
    # （1）xxx  /  (1) xxx  —— 括号编号在行首
    re.compile(r"^\s*[（(]\s*([0-9一二三四五六七八九十]+)\s*[）)]\s*(.+)$"),
    # ①xxx  /  1. xxx  /  1、xxx
    re.compile(r"^\s*([①-⑳])\s*(.+)$"),
    re.compile(r"^\s*(\d+)\s*[.、．]\s*(.+)$"),
]

# 对应的答案标签。
# ⚠ 两种语序都要认（这是踩过的坑）：
#     数字在前： 「（1）答案：」「①答案：」「1. 答案：」
#     数字在后： 「答案1：」「第1问答案：」「小问1答案：」
# ⚠ 关键词里**不能**放"解析"——原文末尾的「解析：」是整题解析，不是答案。
#     （放过一次：答案2 被"解析："抢走，采分点里混进了解析文字。）
_ANS_KW = (r"(?:参考答案|答案|评分标准|得分点|采分点)")
_ANS_FORMS = [
    # 数字在关键词前
    re.compile(rf"^\s*[（(]\s*([0-9一二三四五六七八九十]+)\s*[）)]\s*{_ANS_KW}"
               r"\s*[：:]?\s*(.*)$"),
    re.compile(rf"^\s*([①-⑳])\s*{_ANS_KW}\s*[：:]?\s*(.*)$"),
    re.compile(rf"^\s*(\d+)\s*[.、．]\s*{_ANS_KW}\s*[：:]?\s*(.*)$"),
    # 数字在关键词后：「答案1：」「第1问答案：」「小问1答案：」
    re.compile(rf"^\s*{_ANS_KW}\s*[（(]?\s*([0-9一二三四五六七八九十]+)\s*[）)]?\s*"
               rf"{_ANS_KW}?\s*[：:]?\s*(.*)$"),
    # 数字在关键词后但不带冒号：「答案1 洞穴内存在…」
    re.compile(rf"^\s*{_ANS_KW}\s*[（(]?\s*([0-9一二三四五六七八九十]+)\s*[）)]?\s*"
               rf"(.{{2,}})$"),
    # ⚠ 完全不带编号的：「参考答案：」「第1问答案：」「小问1的答案：」
    #   没有编号 → 归给"最近的那个小问"（no=0）
    re.compile(rf"^\s*(?:第\s*[0-9一二三四五六七八九十]+\s*[问小]?\s*)?"
               rf"(?:小问\s*[0-9一二三四五六七八九十]+\s*的?\s*)?"
               rf"{_ANS_KW}\s*[：:]?\s*(.*)$"),
]

# 总分：满分：12分  /  （12分）  /  本题满分 12 分
_TOTAL_PATTERNS = [
    re.compile(r"^\s*满分\s*[：:]\s*(\d+(?:\.\d+)?)\s*分"),
    re.compile(r"本题\s*满分\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分"),
    re.compile(r"^\s*[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*$"),
]

# 尾部说明：（每点2分，共6分，顺序错误不得分）—— 不是采分点，要剥掉
_TAIL_NOTE = re.compile(
    r"[（(]\s*(?:每点|每题|每小题|每采分点|共|合计|满分|顺序|答对|"
    r"每点得|每点分)[^）)]*[）)]\s*$")

# 尾部说明也可能不在括号里："每点2分，共6分。"
_TAIL_NOTE_LOOSE = re.compile(
    r"(?:^|[；;。\s])(?:每点\s*\d+(?:\.\d+)?\s*分|共\s*\d+(?:\.\d+)?\s*分|"
    r"顺序错误不得分|答对得\d+分)[^。；]*$")

# 材料标签
_MATERIAL_LABELS = ("材料", "材料一", "材料二", "材料三", "阅读材料", "题干材料")

# 主题 / 解析（整题级别）
_TOPIC_PATTERNS = [re.compile(r"^\s*(?:主题|专题|知识点)\s*[：:]\s*(.+)$")]
_GLOBAL_ANALYSIS = re.compile(r"^\s*(?:解析|说明|备注)\s*[：:]\s*(.+)$")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(s: str) -> int:
    s = s.strip()
    if s in _CN_NUM:
        return _CN_NUM[s]
    try:
        return int(s)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

class ParsedSub:
    """一个小问 = 一道独立的题。"""

    def __init__(self) -> None:
        self.no: int = 1                  # 小问序号（1、2…）
        self.stem: str = ""               # 设问（不含"（6分）"）
        self.score_hint: Optional[float] = None   # 原文标的小问分值
        self.answer_raw: str = ""         # 标准答案原文
        self.points: List[Dict] = []      # 采分点 [{seq,text,score,kind}]

    @property
    def max_score(self) -> float:
        """小问满分：优先原文标的，否则按采分点数 × 2。"""
        if self.score_hint and self.score_hint > 0:
            return float(self.score_hint)
        if self.points:
            return round(sum(p["score"] for p in self.points), 2)
        return DEFAULT_POINT_SCORE * 2

    def max_score_source(self) -> str:
        return "written" if (self.score_hint and self.score_hint > 0) \
            else "inferred"


class ParsedFile:
    """一个 txt 解析的结果（可能一道题，可能多道）。"""

    def __init__(self) -> None:
        self.topic: str = ""
        self.material: str = ""
        self.total_score: Optional[float] = None
        self.analysis: str = ""
        self.subs: List[ParsedSub] = []
        self.has_answer: bool = False
        self.warnings: List[str] = []

    def ok(self) -> bool:
        return bool(self.subs) and all(s.stem for s in self.subs)


# ---------------------------------------------------------------------------
# 采分点切分
# ---------------------------------------------------------------------------

def split_points(answer: str, per_point: Optional[float] = None) -> List[Dict]:
    """把一段标准答案切成采分点。

    认三种常见形态（按优先级）：
      1. 一行一条：  "1. xxx" / "① xxx" / "- xxx"
      2. 分号分隔：  "xxx；yyy；zzz"   ← 高考标准答案最常见的写法
      3. 兜底：      整段当成一条

    ⚠ 尾部说明（每点2分，共6分）先剥掉，否则会多切一条出来。
    """
    if not answer or not answer.strip():
        return []

    text = answer.strip()
    # 剥尾部说明（可能连续剥两次："（每点2分，共6分，顺序错误不得分）"）
    for _ in range(3):
        new = _TAIL_NOTE.sub("", text).strip()
        if new == text:
            break
        text = new
    new = _TAIL_NOTE_LOOSE.sub("", text).strip().rstrip("。；;，,")
    if new:
        text = new

    if not text:
        return []

    # 形态 1：行首编号
    pts: List[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:\d+[.、．]|[①-⑳]|[-—•·])\s*(.+)$", s)
        if m:
            pts.append(m.group(1).strip())
        elif pts:
            # 续行并到上一条
            pts[-1] += s
        else:
            pts.append(s)
    if len(pts) >= 1 and _looks_numbered(text):
        return _build(pts, per_point)

    # 形态 2：分号分隔（整段一行）
    if "；" in text or ";" in text:
        segs = [x.strip() for x in re.split(r"[；;]", text) if x.strip()]
        # 分号切出来的段里如果又出现编号，说明是"分号+编号"混排
        cleaned = []
        for s in segs:
            m = re.match(r"^(?:\d+[.、．]|[①-⑳])\s*(.+)$", s)
            cleaned.append(m.group(1).strip() if m else s)
        if len(cleaned) >= 2:
            return _build(cleaned, per_point)

    # 形态 3：只有一条
    return _build([text], per_point)


def _looks_numbered(text: str) -> bool:
    first = text.strip().split("\n")[0]
    return bool(re.match(r"^(?:\d+[.、．]|[①-⑳]|[-—•·])\s*", first))


def _build(parts: List[str], per_point: Optional[float]) -> List[Dict]:
    """把切好的文本变成采分点列表，逐条剥掉自己的分值标记。"""
    out: List[Dict] = []
    for i, raw in enumerate(parts, start=1):
        t = raw.strip()
        if not t:
            continue
        sc = per_point if per_point else DEFAULT_POINT_SCORE
        # 该点自带分值："xxx（3分）" / "xxx(1分)"
        m = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*$", t)
        if m:
            sc = float(m.group(1))
            t = t[:m.start()].strip()
        if not t:
            continue
        out.append({
            "seq": len(out) + 1,
            "text": t,
            "score": sc,
            "kind": "chain" if ("：" in t or "，" in t) else "fact",
        })
    return out


# ---------------------------------------------------------------------------
# 主解析
# ---------------------------------------------------------------------------

def parse_file(text: str) -> ParsedFile:
    """把一段原文解析成 ParsedFile。

    完全本地、**不调 AI**。这个格式（"小问1（6分）：" + "答案1："）能认出来。
    认不出来的格式 import_questions.py 会转交 AI。
    """
    pf = ParsedFile()
    lines = text.replace("\r\n", "\n").split("\n")

    # ---- 逐行扫，识别标签 ----
    material_parts: List[str] = []
    cur_sub: Optional[ParsedSub] = None
    cur_ans: Optional[str] = None        # 当前正在收集答案
    pending_topic = ""
    pending_analysis: List[str] = []

    def close_answer():
        """把收集到的答案行落到**真正对应的小问**上。

        ⚠ 踩过的坑：答案可能归给 cur_sub 之外的另一个小问
        （比如「答案1：」出现在小问2 之后，或者标签不带编号时
        落到"最近的小问"）。所以要记 cur_ans_owner，不能想当然用 cur_sub ——
        踩过一次：内容写进了小问1，导致小问1 的设问被答案覆盖、采分点错位。
        """
        nonlocal cur_ans
        owner = cur_ans.get("sub") if isinstance(cur_ans, dict) else None
        lines_ = cur_ans.get("lines") if isinstance(cur_ans, dict) else None
        if owner is not None and lines_:
            owner.answer_raw = "\n".join(lines_).strip()
            owner.points = split_points(owner.answer_raw)
        cur_ans = None

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()

        if not s:
            i += 1
            continue

        # 主题
        hit = False
        for pat in _TOPIC_PATTERNS:
            m = pat.match(s)
            if m:
                pending_topic = m.group(1).strip()
                hit = True
                break
        if hit:
            i += 1
            continue

        # 总分（"满分：12分" 单独一行）
        for pat in _TOTAL_PATTERNS:
            m = pat.match(s)
            if m:
                pf.total_score = float(m.group(1))
                hit = True
                break
        if hit:
            i += 1
            continue

        # 材料
        for lab in _MATERIAL_LABELS:
            if s.startswith(lab) and (len(s) == len(lab) or s[len(lab)] in "：:"):
                material_parts.append(s[len(lab):].lstrip("：: "))
                i += 1
                break
        else:
            # 小问标题？
            sub = _match_sub_head(s)
            if sub is not None:
                close_answer()
                cur_sub = ParsedSub()
                cur_sub.no = sub["no"]
                cur_sub.stem = sub["stem"]
                cur_sub.score_hint = sub["score"]
                pf.subs.append(cur_sub)
                i += 1
                continue

            # 答案标签？
            ans = _match_answer(s)
            if ans is not None:
                close_answer()
                # no=0 表示标签没带编号 → 归给"最近的那个小问"
                target = _sub_by_no(pf, ans["no"]) if ans["no"] else cur_sub
                if target is None:
                    # 连小问都没有：造一个（原文没写设问，只有答案）
                    target = ParsedSub()
                    target.no = len(pf.subs) + 1
                    pf.subs.append(target)
                    cur_sub = target
                cur_ans = {"sub": target,
                           "lines": [ans["rest"]] if ans["rest"] else []}
                i += 1
                continue

            # 整题级解析：「解析：」「说明：」——永远不当答案
            m = _GLOBAL_ANALYSIS.match(s)
            if m and not _ANSWER_WORDS.search(s):
                pending_analysis.append(m.group(1).strip())
                i += 1
                continue

            # 续行
            if isinstance(cur_ans, dict):
                # ⚠ 答案已经收尾、后面又来普通行 → 不该并进答案
                #   （比如"解析："这行如果被 _GLOBAL_ANALYSIS 漏掉，
                #     就会污染最后一个小问的答案，把解析当成采分点）
                if _is_continuation(s):
                    cur_ans["lines"].append(s)
                else:
                    close_answer()
                    if pf.subs:
                        pf.subs[-1].stem += s
                    else:
                        material_parts.append(s)
            elif pf.subs:
                # 小问设问的续行
                pf.subs[-1].stem += s
            else:
                material_parts.append(s)
        i += 1

    close_answer()

    pf.topic = pending_topic
    pf.material = "\n".join(material_parts).strip()
    pf.analysis = " ".join(pending_analysis).strip()
    pf.has_answer = any(s.points or s.answer_raw for s in pf.subs)

    # ---- 校验与警告 ----
    if not pf.subs:
        pf.warnings.append("没识别出任何小问（可能格式不一样，需要 AI 兜底）")
        return pf
    if not pf.material:
        pf.warnings.append("没识别到材料")
    for s in pf.subs:
        if not s.stem:
            pf.warnings.append(f"第{s.no}小问没识别出设问")
        if not s.points:
            pf.warnings.append(
                f"第{s.no}小问没识别出采分点（原文可能没答案）")
        else:
            need, acc = 0, 0.0
            for p in s.points:
                if acc >= s.max_score:
                    break
                acc += p["score"]
                need += 1
            if need < len(s.points):
                pf.warnings.append(
                    f"第{s.no}小问共 {len(s.points)} 个采分点，"
                    f"{need} 个就到满分 {s.max_score:g} 分，"
                    f"多出的 {len(s.points) - need} 个作候补")

    # 整题总分 vs 各小问之和
    if pf.total_score:
        s_sum = sum(s.max_score for s in pf.subs)
        if abs(s_sum - pf.total_score) > 0.01 and pf.subs:
            pf.warnings.append(
                f"原文写满分 {pf.total_score:g} 分，但各小问加起来是 {s_sum:g} 分"
                f"（以各小问为准）")

    return pf


def _match_sub_head(s: str) -> Optional[Dict]:
    for pat in _SUB_HEADS:
        m = pat.match(s)
        if m:
            groups = m.groups()
            if len(groups) == 3 and groups[1]:      # 小问1（6分）：xxx
                no = _to_int(groups[0])
                try:
                    sc = float(groups[1])
                except ValueError:
                    sc = None
                stem = groups[2].strip()
            else:                                     # （1）xxx / ①xxx / 1. xxx
                no = _to_int(groups[0])
                sc = None
                stem = groups[-1].strip()
            if no <= 0:
                continue
            # 设问里别带分值
            stem = re.sub(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]", "", stem).strip()
            return {"no": no, "score": sc, "stem": stem}
    return None


def _match_answer(s: str) -> Optional[Dict]:
    for pat in _ANS_FORMS:
        m = pat.match(s)
        if m:
            groups = m.groups()
            return {"no": _to_int(groups[0]), "rest": groups[-1].strip()}
    return None


def _sub_by_no(pf: ParsedFile, no: int) -> Optional[ParsedSub]:
    for s in pf.subs:
        if s.no == no:
            return s
    return None


# 这些词出现在行首时，说明这行**不是**答案的续行（是新的一段说明）
_ANSWER_WORDS = re.compile(
    r"^\s*(?:答案|参考答案|评分标准|得分点|采分点|解析|说明|备注|点评|"
    r"分析|小结|结论|注意|本题|思路|方法|点拨)")

# 答案的合法续行：非空、不以说明性词开头
def _is_continuation(s: str) -> bool:
    if not s.strip():
        return False
    return not _ANSWER_WORDS.match(s)


# ---------------------------------------------------------------------------
# 转成 import_questions.py 用的 dict（和 AI 返回的结构一致）
# ---------------------------------------------------------------------------

def to_dicts(pf: ParsedFile) -> List[Dict]:
    """把解析结果转成"一个 dict 一道题"（拆成多道独立题）。

    每道题都带完整材料 + 自己那一份答案 + 自己的采分点。
    """
    out: List[Dict] = []
    mat = pf.material
    for s in pf.subs:
        out.append({
            "ok": bool(s.stem and s.points),
            "sub_no": s.no,
            "topic": pf.topic,
            "stem": s.stem,
            "material": mat,
            "has_answer": bool(s.points),
            "max_score": s.max_score,
            "max_score_source": s.max_score_source(),
            "points": s.points,
            "analysis": pf.analysis,
            "unclear_parts": list(pf.warnings),
        })
    return out
