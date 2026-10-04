"""题库试题解析：把教师自己维护的 txt 读成结构化的题目。

【产品逻辑 2026-10-04】
题目不再由 AI 随机出，全部来自教师维护的 `题库/*.txt`。
多小问的大题**拆成多道独立题**，每道题都带同一段材料。

【标准格式】每行一个标签，方括号包住（【】 或 [] 或 都可以）
    【主题】地表形态的塑造
    【满分】12
    【材料】盐风化是指岩石……
    【小问1】
    【设问】推测流纹岩的形成过程。
    【分值】6
    【答案】甲；乙；丙
    【小问2】
    【设问】简述判断依据。
    【分值】6
    【答案】丁；戊；己
    【解析】本题考查……

规则（故意做得很死，不做模糊猜测）：
    · 【主题】【满分】【解析】可以省略
    · 【分值】省略就按「采分点 × 2」算（每点默认 2 分）
    · 【答案】用「；」分隔采分点；尾部「（每点2分，共6分）」这类说明自动剥掉
    · 【材料】【解析】可以跨行（写到下一个标签为止）
    · 认不出来 → import_questions.py 转交 AI 兜底

为什么用【标签】而不是"题目：xxx"这种：
    标签独占一行、内容在标签后面，**没有歧义**。
    用「小问1（6分）：xxx」这种把编号、分值、设问挤在一行的写法，
    两种格式长得太像，解析容易串味（实测踩过）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

DEFAULT_POINT_SCORE = 2.0

# 标签。用 (?:【|\\[|\(|（)? … (?:】|\\]|\\)|）)? 包住，
# 这样 【主题】/【主题】/[主题] 都能认，但**要求标签在行首**（前面只允许空白）。
_TAG = r"[【\[（(]\s*{name}\s*[】\]）)]"
_ANY = r"[【\[（(](.*?)[】\]）)]"


def _tag(name: str) -> re.Pattern:
    """生成"整行就是这个标签"的正则。"""
    return re.compile(r"^\s*" + _TAG.format(name=name) + r"\s*$")


def _field(name: str) -> re.Pattern:
    """生成"标签 + 同行内容"的正则（内容允许为空）。"""
    return re.compile(r"^\s*" + _TAG.format(name=name) + r"\s*(.*)$")


# 所有标签统一用 _field("名字") 现场生成正则匹配，
# 这样"标签独占一行"和"标签+内容同一行"用的是同一套逻辑，不会漏。

# 【小问1】/【小问1：】/【小问1】后面可带内容（当设问用，省一行）
RE_SUB = re.compile(
    r"^\s*[【\[（(]\s*小问\s*([0-9]+)\s*[】\]）)]\s*(.*)$")
RE_STEM = _field("设问")
RE_ANSWER = _field("答案")

# 答案尾部说明：（每点2分，共6分，顺序错误不得分）
RE_TAIL_NOTE = re.compile(
    r"[（(]\s*(?:每点|每题|每小题|每采分点|共|合计|满分|顺序|答对|"
    r"每点得|每点分)[^）)]*[）)]\s*$")

# 某个采分点自带分值：xxx（3分）
RE_POINT_SCORE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*$")

# 任何标签行（用来判断"上一段的文字到此结束"）
RE_ANY_TAG = re.compile(r"^\s*[【\[（(]")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class ParsedSub:
    """一个小问 = 一道独立的题。"""

    def __init__(self, no: int) -> None:
        self.no: int = no
        self.stem: str = ""
        self.score: Optional[float] = None
        self.points: List[Dict] = []
        # 教师自己写的判分注意事项（原文的【评分说明】/【备注】/【说明】）。
        # ⚠ 这段最容易被忽略，但往往是最关键的：例如"只写前两步、
        #   漏掉抬升侵蚀的，总分不得超过 4 分"——这是给 AI 阅卷的硬约束，
        #   丢了它 AI 就会把 6 分的题判成 6 分。
        self.note: str = ""

    @property
    def max_score(self) -> float:
        if self.score and self.score > 0:
            return float(self.score)
        if self.points:
            return round(sum(p["score"] for p in self.points), 2)
        return DEFAULT_POINT_SCORE * 2

    def score_source(self) -> str:
        return "written" if (self.score and self.score > 0) else "inferred"


class ParsedFile:
    def __init__(self) -> None:
        self.topic: str = ""
        self.material: str = ""
        self.total_score: Optional[float] = None
        self.analysis: str = ""
        self.subs: List[ParsedSub] = []
        self.has_answer: bool = False
        self.warnings: List[str] = []
        self.ok: bool = False

    def dicts(self) -> List[Dict]:
        """拆成"一个 dict 一道题"（多小问 → 多道独立题）。"""
        return [{
            "ok": bool(s.stem and s.points),
            "sub_no": s.no,
            "topic": self.topic,
            "stem": s.stem,
            "material": self.material,
            "has_answer": bool(s.points),
            "max_score": s.max_score,
            "max_score_source": s.score_source(),
            "points": s.points,
            "analysis": self.analysis,
            "note": s.note,
        } for s in self.subs]


# ---------------------------------------------------------------------------
# 采分点切分
# ---------------------------------------------------------------------------

def split_points(answer: str) -> List[Dict]:
    """切采分点：剥尾部说明 → 按「；」和换行切 → 逐条剥自己的分值。"""
    if not answer or not answer.strip():
        return []
    text = answer.strip()
    for _ in range(3):                      # 尾部说明可能套多层
        new = RE_TAIL_NOTE.sub("", text).strip()
        if new == text:
            break
        text = new

    out: List[Dict] = []
    for part in re.split(r"[；;\n]", text):
        t = part.strip()
        # 去掉行首编号（"1." "①" "-"），教师可能顺手写上
        t = re.sub(r"^(?:\d+\s*[.、．]|[①-⑳]|[-—•·])\s*", "", t).strip()
        if not t:
            continue
        sc = DEFAULT_POINT_SCORE
        m = RE_POINT_SCORE.search(t)
        if m:
            try:
                sc = float(m.group(1))
            except ValueError:
                sc = DEFAULT_POINT_SCORE
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
# 主解析：状态机
# ---------------------------------------------------------------------------

# 当前正在收集内容的字段
_FIELD_NONE = 0
_FIELD_MATERIAL = 1
_FIELD_ANALYSIS = 2
_FIELD_ANSWER = 3
_FIELD_NOTE = 4


def parse_file(text: str) -> ParsedFile:
    """按【标签】格式解析。认不出来就在 warnings 里说明。"""
    pf = ParsedFile()
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]

    subs: Dict[int, ParsedSub] = {}
    field = _FIELD_NONE            # 当前在收哪个字段的内容
    buf: List[str] = []             # 该字段的续行
    cur_sub: Optional[ParsedSub] = None   # 正在收【答案】的那个小问
    last_sub: Optional[ParsedSub] = None   # 最近读到的小问（收【设问】时用）

    def end_field():
        """把 buf 里的内容落到对应的字段上。"""
        nonlocal field, buf, cur_sub
        text_ = "\n".join(buf).strip()
        if field == _FIELD_MATERIAL:
            if text_:
                pf.material = (pf.material + "\n" + text_) if pf.material \
                    else text_
        elif field == _FIELD_ANALYSIS:
            if text_:
                pf.analysis = (pf.analysis + " " + text_) if pf.analysis \
                    else text_
        elif field == _FIELD_ANSWER and cur_sub is not None:
            cur_sub.points = split_points(text_)
        elif field == _FIELD_NOTE:
            # 归属"最近读到的小问"；【评分说明】通常紧跟在【答案】后面，
            # 这时 cur_sub 就是它，所以两个都兜住。
            target = cur_sub or last_sub
            if target is not None and text_:
                target.note = (target.note + " " + text_) if target.note \
                    else text_
        buf = []
        field = _FIELD_NONE

    for raw in lines:
        s = raw.strip()
        if not s:
            continue

        # ---------- 标签行 ----------
        # 注意：【材料】【答案】等可能"标签 + 内容写在同一行"，
        #   所以先试"同行带内容"的匹配，剩下的才当成纯标签行。
        # ⚠ _field() 只认标签**紧跟**内容的情况（标签闭合后必须没别的字），
        #   纯标签行【材料】单独一行时 group(1) 是空串，也走这条路 ——
        #   所以不会漏。
        m = _field("材料").match(s)
        if m:
            end_field()
            field = _FIELD_MATERIAL
            body = m.group(1).strip()
            if body:
                buf = [body]
            continue

        m = _field("解析").match(s)
        if m:
            end_field()
            field = _FIELD_ANALYSIS
            body = m.group(1).strip()
            if body:
                buf = [body]
            continue

        # 【评分说明】/【评分备注】/【判分说明】/【说明】/【备注】
        # 教师自己写的判分注意事项，归属最近读到的小问。
        # ⚠ 必须排在下面"续行"判断之前，否则会被当成陌生标签整段丢掉。
        #   （实测踩过：用户写了"漏掉抬升侵蚀的总分不得超过4分"，
        #     解析器不认识这个标签，AI阅卷时看不到这条硬约束。）
        # 写成循环是为了认这几个近义标签；用独立变量，别借用 m ——
        # m 后面还要给 RE_SUB / RE_STEM 用，借了会被覆盖。
        note_hit = None
        for _alt in ("评分说明", "评分备注", "判分说明", "说明", "备注"):
            note_hit = _field(_alt).match(s)
            if note_hit:
                break
        if note_hit:
            end_field()
            field = _FIELD_NOTE
            body = note_hit.group(1).strip()
            if body:
                buf = [body]
            continue

        m = RE_SUB.match(s)
        if m:
            end_field()
            no = int(m.group(1))
            sub = subs.get(no)
            if sub is None:
                sub = ParsedSub(no)
                subs[no] = sub
            last_sub = sub
            # 【小问1】后面直接跟内容 → 当设问（省一行【设问】）
            inline = m.group(2).strip()
            if inline:
                sub.stem = inline
            cur_sub = sub
            continue

        m = RE_STEM.match(s)
        if m:
            end_field()
            if last_sub is not None:
                last_sub.stem = m.group(1).strip()
            continue

        m = RE_ANSWER.match(s)
        if m:
            end_field()
            if cur_sub is None:
                # 【答案】前面没有【小问N】→ 造一个
                cur_sub = ParsedSub(len(subs) + 1)
                subs[cur_sub.no] = cur_sub
                last_sub = cur_sub
            field = _FIELD_ANSWER
            body = m.group(1).strip()
            if body:
                buf = [body]
            continue

        # 【分值】6  —— 跟在某个小问后面，归属最近读到的小问
        m = _field("分值").match(s)
        if m:
            end_field()
            body = m.group(1).strip().rstrip("分").strip()
            if body and last_sub is not None:
                try:
                    last_sub.score = float(body)
                except ValueError:
                    pass
            continue

        m = _field("满分").match(s)
        if m:
            end_field()
            body = m.group(1).strip().rstrip("分").strip()
            if body:
                try:
                    pf.total_score = float(body)
                except ValueError:
                    pass
            continue

        m = _field("主题").match(s)
        if m:
            end_field()
            pf.topic = m.group(1).strip()
            continue

        # ---------- 续行 ----------
        # 遇到任何标签行就先收尾（标签不认识也不该当成正文）
        if RE_ANY_TAG.match(s):
            end_field()
            continue
        if field != _FIELD_NONE:
            buf.append(s)

    end_field()

    # 收尾
    pf.subs = [subs[k] for k in sorted(subs)]
    pf.has_answer = any(s.points for s in pf.subs)
    _validate(pf)
    return pf


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate(pf: ParsedFile) -> None:
    """挑毛病，并判断能不能"直接用"（不能就转交 AI）。"""
    hard: List[str] = []

    if not pf.material:
        hard.append("没读到【材料】")
    if not pf.subs:
        hard.append("没读到任何【小问1】")

    for s in pf.subs:
        if not s.stem:
            hard.append(f"小问{s.no} 没读到【设问】")
        if not s.points:
            hard.append(f"小问{s.no} 没读到【答案】（或答案是空的）")
        else:
            need, acc = 0, 0.0
            for p in s.points:
                if acc >= s.max_score:
                    break
                acc += p["score"]
                need += 1
            if need < len(s.points):
                pf.warnings.append(
                    f"小问{s.no} 有 {len(s.points)} 个采分点，"
                    f"{need} 个就到满分 {s.max_score:g} 分，"
                    f"多出的 {len(s.points) - need} 个作候补（答对也不加分）")

    if pf.total_score and pf.subs:
        s_sum = sum(s.max_score for s in pf.subs)
        if abs(s_sum - pf.total_score) > 0.01:
            pf.warnings.append(
                f"原文写满分 {pf.total_score:g}，各小问加起来 {s_sum:g}"
                f"（以各小问为准）")

    pf.warnings.extend(hard)
    pf.ok = bool(pf.subs and pf.material and not hard)
