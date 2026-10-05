"""生成质量闸门：检查 AI 写的答卷有没有"该答浅却答到位"。

为什么需要这道闸门
──────────────────
AI 生成学生答案时手里就有完整评分细则，天生倾向于照着写满——
即使逐点叮嘱"这一点只能写表面、因果链要丢掉"，它还是会把采分点原样写上去。
光靠提示词压不住，所以这里做一次**机械校验**，三层：

  ① 核对自述（主力判据）
     AI 必须先声明"这一点我要漏掉哪一段因果"，并用「」标出来。
     本地就去卷面里查那段话是不是真的没出现——出现了就打回重写。
     这一步同时要求"声明出来的话必须原样出自采分点表述"，
     防止它随便声明一段不相干的字来蒙混过关。

  ② 文本比对（兜底）
     把答案与该采分点的标准表述做二元组（bigram）重合度、
     以及"最长连续雷同片段"比对，超线即判定"写到点子上"了。
     草草了事、未作答的点用得上；沾边点因为条件本来就来自材料，
     门槛放得很宽，只拦"几乎原样搬了整条细则"的情况。

  ③ 方向红线（本次新增）
     题目问自然条件，答案里冒出"经济、人口、水能"；问有利条件，
     答案里冒出"崎岖、不足、制约"——这不是"答得浅"，是"乱写"，
     卷子直接作废。违禁词表放在 config.yaml 里，可自己加减。
     ⚠ 只在题目问法明确时启用：问"自然"且没问"人文"才启用自然词表；
       问"有利"且没问"不利"才启用否定词表，避免误伤两问的题。

校验是纯本地计算，不调用 AI，不花钱，也不会误伤"答对"的点。

【本文件里的"物理删除"逻辑已停用】
`force_fix()` / `_degrade()` 那套"本地硬删硬改"的办法会在卷面上留下
一眼能看出来的机器痕迹（断头句、光杆介词），方向修正后改用
"让 AI 把那一句改浅／改笼统"（见 answer_fix.txt）来制造缺陷。
这些函数保留在文件里但默认不调用，由 config.yaml 的 `classroom.force_fix`
开关控制（默认 false = 关闭）。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Question, StudentPlan

# 非实义字符（标点、空白、数字）一律去掉，避免"705万"这类数字干扰
_NOISE = re.compile(r"[\s\d，。、；：？！（）()【】\[\]“”\"'·\-—…,.:;?!]")

# 判定阈值一：答案与"该点标准表述"的 bigram 重合度上限
#   未作答的点要求最严（整条不写）；
#   草草了事、沾边的点放得很宽——它们写的都是采分点【冒号前那半句条件】，
#   那半句本来就来自题目材料，重合度天然不低，卡太严会天天误报。
#   这一条只用来拦"把因果链也一起抄了"（差不多等于原样搬了整条细则）。
THRESHOLDS: Dict[str, float] = {
    "blank": 0.30,
    "off": 0.75,
    "partial": 0.75,
}

# 判定阈值二：与标准表述"连续雷同"多少字就算写到了点子上。
# 这一条更关键——答案常常只抄采分点的后半段，
# 整体重合度会被前半段稀释到 0.3 左右，但连续雷同片段一眼就能抓出来。
# ⚠ 部分命中（partial）给到 10 字，而且只跟采分点【冒号后的因果链】比：
#   条件那半句材料里就有、本来就允许写。实测踩过（第 18 号存档）：
#   产业题的条件是"高校培养大量电子信息专业人才"这种专有长名词，
#   学生照实写条件就和细则连续雷同 17 字 → 天天误报，
#   而 AI 改写时根本绕不开专有名词 → "改写后没有改善"死循环。
#   把比对范围缩到因果链后，这个坑就填平了。
MIN_COMMON: Dict[str, int] = {
    "blank": 5,
    "off": 7,
    "partial": 10,
}


def _causal_half(point_text: str) -> str:
    """取采分点「冒号后」的因果机制 + 落脚结论。

    采分点的统一格式是"条件／现象：因果机制、落脚结论"。
    部分命中的学生允许写条件那半句（材料里就有），
    所以文本比对只能对着因果链比，不能拿全文比——
    否则照实写条件的答卷会被误判成"照抄标准答案"。
    没有冒号的点（少见）退回全文。
    """
    for sep in ("：", ":"):
        if sep in point_text:
            tail = point_text.split(sep, 1)[1].strip()
            if tail:
                return tail
    return point_text

# ── YAML 的坑：没加引号的 off 会被解析成布尔值 False ────────────────────
# YAML 里 on/off/yes/no/true/false 都算布尔值。config.yaml 里写着
#   similarity_thresholds:
#     off: 0.45
# 读进来键名其实是 False 而不是 "off"，于是 `th.get("off")` 取不到、
# 悄悄退回默认值——用户在配置文件里改了也白改，还找不到原因。
# 这里统一把键名掰回字符串，配置怎么写都能认出来。
_BOOL_KEY_ALIAS = {False: "off", True: "on"}


def normalize_keys(d: Dict) -> Dict:
    """把配置字典的键统一成小写的字符串（顺便还原 on/off 的布尔坑）。"""
    out: Dict = {}
    for k, v in (d or {}).items():
        if isinstance(k, bool):
            k = _BOOL_KEY_ALIAS[k]
        out[str(k).strip().lower()] = v
    return out

# ── 撤掉过的一条判据，记在这里免得下次又想加 ──────────────────────────
# 曾想再加一条"采分点里的两字词原样命中了几个"来抓**同义改写**
# （学生把"湖泊众多、河道弯曲"写成"湖泊星罗棋布、河道蜿蜒曲折"——
#  换掉动词后重合度只有 0.18、连续雷同只有 2 字，上面两条都抓不到）。
# 实测它确实能抓到这一例，但**误报更多**：同一道大题里各采分点共用词汇
# （"夏季""集中""流域""水量"），真正跑偏的答案也会撞上 3~4 个词，
# 于是每轮都误判、逼着 AI 白跑一次改写——白花 API 额度。
# 结论：纯字面办法做不到这一层，交给提示词去管（answer_system.txt 里
# 已明确写"被标为答非所问的点，采分点里的名词一个都不要用"）。
# 这一层若仍漏，属于已知残留问题，见 USER_GUIDE.md。


def _normalize(text: str) -> str:
    return _NOISE.sub("", text or "")


def _normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """归一化，同时记住每个字在原文里的下标（用于精确定位、回删）。"""
    chars: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(text or ""):
        if _NOISE.match(ch):
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def bigrams(text: str) -> set:
    s = _normalize(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def similarity(point_text: str, answer: str) -> float:
    """答案与某个采分点标准表述的重合度，0~1。"""
    base = bigrams(point_text)
    if not base:
        return 0.0
    hit = base & bigrams(answer)
    return len(hit) / len(base)


def longest_common_span(point_text: str, answer: str) -> Tuple[int, int, int]:
    """最长连续雷同片段的 (长度, 起点, 终点)，坐标是**归一化后 answer** 的下标。"""
    a = _normalize(point_text)
    b, _ = _normalize_with_map(answer)
    if not a or not b:
        return 0, -1, -1
    prev = [0] * (len(b) + 1)
    best: Tuple[int, int, int] = (0, -1, -1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, start=1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best[0]:
                    best = (cur[j], j - cur[j], j)
        prev = cur
    return best


def longest_common(point_text: str, answer: str) -> int:
    """答案中最长的"与标准表述连续雷同"的片段长度（字数）。

    这是抓"照抄"最灵敏的指标：哪怕只抄了采分点的后半段，
    连续雷同片段也会很长，而整体 bigram 重合度会被稀释。
    """
    return longest_common_span(point_text, answer)[0]


def _required_run(point_text: str, common_limit: int, cap: int = 10) -> int:
    """判定"照抄"所需的最短连续雷同长度（随采分点长短浮动）。

    为什么不固定成 6 字：采分点长短差很多。
      · 短点（"无结冰期可全年通航"，9 字）——连续 6 字雷同基本就是照抄；
      · 长点（"长江中下游地势低平，排水缓慢，水位上涨持续时间长"，22 字）
        ——学生只引用了其中 6 个字，那恰恰是"沾边但没答到"的半对答案，
        不该被判成满分。
    门槛取"采分点长度的 40%"，并限制在 [common_limit, cap] 之间。
    定 40% 而不是 50% 是实测调出来的：50% 时，学生把长采分点里
    7~8 个字（如"降水丰富且集中"）原样搬过来也判不出照抄。
    """
    n = len(_normalize(point_text))
    want = max(common_limit, int(round(n * 0.4)))
    return min(want, max(common_limit, cap))


def _term_leaked(term: str, text: str) -> bool:
    """自述里声明"要漏掉"的词，是否变相出现在答案里。

    不能只用 `term in text`：AI 很会钻空子——
    声明"漏掉「季节分配均匀」"，实际写成"季节分配【相对】均匀"，
    插两个字就绕过了精确匹配。所以再加一层二元组重合度判定。
    """
    if not term:
        return False
    if term in text:
        return True
    if len(_normalize(term)) < 4:
        return False
    return similarity(term, text) >= 0.75


def _in_point(term: str, point_text: str) -> bool:
    """声明"要漏掉"的那段话，是不是确实出自采分点的表述。

    为什么要查这一步：沾边的核对全靠"这段话有没有出现在答案里"，
    如果允许 AI 随便声明一段采分点之外的文字（比如给"自然成因"这一点
    声明「经济」，然后照抄标准答案），这道闸门就等于没有。
    所以要求「」里必须是采分点表述里**原样连着的一段字**。
    """
    t = _normalize(term)
    return bool(t) and t in _normalize(point_text)


# ── 方向红线：答案里不许出现"跑题方向"的词 ────────────────────────────
# 题目问自然条件，答案里冒出"经济、人口、水能"；问有利条件，
# 答案里冒出"崎岖、不足、制约"——这种答案不是"答得浅"，是"乱写"，
# 拿给教师练批改会把整个产品带偏。词表在 config.yaml 里（用户可改）。
#
# ⚠ 为什么不能无脑套用：词的意思跟题目方向有关。
#   "热量不足"在"分析不利条件"的题里就是标准答案；
#   "交通便利"在"分析人文区位"的题里也是标准答案。
#   所以只在题目问法明确时启用，而且两问的题（"评价有利与不利影响"）
#   一律不启用，宁可漏判也不误伤。
_ASK_RE = re.compile(r"【设问】(.*)$", re.S)

# 设问里出现这些词，说明问的是哪个方向
_NEG_ASK = ("不利", "危害", "问题", "劣势", "限制性")
_HUMAN_ASK = ("人文", "社会", "经济")


def question_direction(question: Question) -> List[str]:
    """判断这道题问的是哪个方向，返回要启用的违禁词表名字。

    只看【设问】部分——材料里写什么都不算，避免材料提到"经济"就误判。
    拿不准就返回空列表（不启用任何词表），宁可漏判也不误伤。
    """
    stem = question.stem or ""
    m = _ASK_RE.search(stem)
    ask = m.group(1) if m else stem

    out: List[str] = []
    if "自然" in ask and not any(w in ask for w in _HUMAN_ASK):
        out.append("natural")
    if "有利" in ask and not any(w in ask for w in _NEG_ASK):
        out.append("favorable")
    return out


def _forbidden_words_for(
    question: Question, forbidden: Optional[Dict[str, Iterable[str]]]
) -> List[str]:
    """按题目方向取出这次该禁用的词，合并成一张表（不分来源）。

    ⚠ 评分细则里出现过的词一律放行。实测踩过：题目问"城市发展空间受限的
    自然原因"，某个采分点里写着"增加了交通连接成本"——"交通"在违禁表里，
    于是答对这个点的学生反而被判"跑方向"。细则里的词是教师认可的，
    它出现在答案里就不算跑题，所以这里先把它们摘掉。
    """
    if not forbidden:
        return []
    rubric = "".join(p.text for p in question.points)
    out: List[str] = []
    for key in question_direction(question):
        for w in forbidden.get(key) or []:
            w = str(w).strip()
            if not w or w in out:
                continue
            if w in rubric:  # 细则自己就用了这个词，放行
                continue
            out.append(w)
    return out


def direction_violations(
    question: Question,
    answer: str,
    forbidden: Optional[Dict[str, Iterable[str]]] = None,
    allowed: int = 0,
) -> List[str]:
    """检查答案里有没有"跟题目方向不符"的违禁词。

    forbidden 形如 {"natural": [...], "favorable": [...]}（来自 config.yaml）。
    返回人类可读的问题列表；空列表表示没有跑方向。

    ⚠ 2026-10-05 新增参数 allowed：**允许有几处故意的方向错误**。
      为什么需要它：现在生成端会按planner.WRONG_QUOTA 故意注入
      「答非所问」类的错误（中等 1 条、薄弱 2 条、很差 2~3 条）来压得分率。
      这道闸门原本见到违禁词就整份打回重写，会把**故意写的错误"修"掉**——
      白烧额度，而且闸门反复打回会让 AI 越改越少写，得分率反而压不下来。
      所以这里按分句计数：违禁词出现次数没超过 allowed 就放过。
      超出配额才算真跑方向，那才是要拦的硬伤。
      allowed 一般传 len(plan.wrong_kinds)，由调用方给。
    """
    if not answer.strip():
        return []

    dirs = question_direction(question)
    if not dirs:
        return []

    rubric = "".join(p.text for p in question.points)
    why = {
        "natural": "题目问的是自然条件／自然原因",
        "favorable": "题目问的是有利条件／优势",
    }
    # 按分句切开数——统计"有几处写了不该有的东西"，
    # 而不是"有几个违禁词"（一处可能顺口带过好几个词）。
    segs = [s for s in re.split(r"[\n；;。]", answer or "") if s.strip()]
    problems: List[str] = []
    for key in dirs:
        words = [str(w).strip() for w in (forbidden.get(key) or []) if str(w).strip()]
        hit_segs: List[str] = []
        n_seg = 0
        for seg in segs:
            if any(w in rubric for w in words):
                # 细则自己用过的词放行（细则里的词是教师认可的，不算跑题）
                continue
            hit_words = [w for w in words if w in seg]
            if hit_words:
                n_seg += 1
                if len(hit_segs) < 5:
                    hit_segs.append(f"{seg.strip()[:30]}（{'／'.join(hit_words[:3])}）")
        if not n_seg:
            continue
        if n_seg <= allowed:
            # 在配额之内 → 这是故意注入的错误，放过
            continue
        problems.append(
            f"方向红线：{why.get(key, '题目问的方向')}，"
            f"答案里有 {n_seg} 处出现了「{'」「'.join(hit_segs[:3])}」"
            f"这类不该有的词（这一档只允许 {allowed} 处）"
        )
    return problems


# 自述里这些是"元说法"，不是关键词，不参与核对
_META_TERMS = {
    "整条不写", "不写", "略过", "不出现", "删除", "缺漏", "答错", "漏掉",
    "未作答", "空白", "只写结论", "不写理由", "一句话", "写浅", "写残",
    "只写条件", "不写因果", "不写机制", "不推因果", "半句",
}
_SEG = re.compile(r"第\s*([1-9]\d*)\s*点\s*[：:，,、]?\s*")
# ⚠ 上限必须给够：新主线要求 AI 声明"要漏掉的那段因果"，
#   而采分点里的因果链常常有 20 多字（"废弃河道中泥沙淤积，逐渐演变为沼泽和牛轭湖"）。
#   上限定成 20 时，长一点的声明会整个匹配不上、被当成"没说"，
#   于是白打回重写一轮（实测踩过，白花额度）。
_QUOTED = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")


def parse_declared(flaw: str) -> Dict[int, List[str]]:
    """从 AI 的自述（flaw 字段）里解析出"第N点 → 它打算漏掉的因果"。

    为什么非要这一步：AI 拿到完整评分细则后，最典型的失败就是
    "嘴上说这条只写表面，笔下照样把因果链写全"。
    让它把要漏掉的那段话用「」标出来后，本地就能机械核对——
    那段话真的不在答案里才算数，出现了就打回重写。
    """
    out: Dict[int, List[str]] = {}
    if not flaw:
        return out
    segs = list(_SEG.finditer(flaw))
    for i, m in enumerate(segs):
        seq = int(m.group(1))
        end = segs[i + 1].start() if i + 1 < len(segs) else len(flaw)
        seg = flaw[m.start() : end]
        bucket = out.setdefault(seq, [])
        for raw in _QUOTED.findall(seg):
            term = raw.strip()
            if term and term not in _META_TERMS and term not in bucket:
                bucket.append(term)
    return out


def find_violations(
    question: Question,
    plan: StudentPlan,
    answer: str,
    thresholds: Dict[str, float] | None = None,
    min_common: Dict[str, int] | None = None,
    flaw: str = "",
    forbidden: Optional[Dict[str, Iterable[str]]] = None,
) -> List[str]:
    """列出这份答卷"不该答到位却答到位了"的地方，供 AI 重写时参考。

    flaw 是 AI 生成时的自述（含「」标出的"我打算漏掉的因果"）。
    传进来后，除了机械比对文本重合度，还会核对它有没有说到做到。

    返回空列表表示合格。
    """
    if not answer.strip():
        return []

    th = dict(THRESHOLDS)
    if thresholds:
        th.update({k: float(v) for k, v in normalize_keys(thresholds).items()
                   if v is not None})

    points = {p.seq: p.text for p in question.points}
    problems: List[str] = []

    common_limits: Dict[str, int] = dict(MIN_COMMON)
    if min_common:
        common_limits.update({k: int(v) for k, v in normalize_keys(min_common).items()})

    # ---- 方向红线先查：跑题的答案不用再看别的 ----
    problems.extend(direction_violations(question, answer, forbidden))

    checks: List[Tuple[List[int], str, str, str]] = [
        (plan.blank, "blank", "整条不写（他没答到这一条）", "答案里却出现了相关内容"),
        (plan.off, "off", "未命中（答不到点子上）", "却写到了点子上"),
        (plan.partial, "partial", "只部分命中（因果链不写）", "却写得跟标准答案几乎一样"),
    ]

    for seqs, key, required, found in checks:
        limit = th.get(key, 0.5)
        common_limit = common_limits.get(key, 8)
        for seq in seqs:
            text = points.get(seq)
            if not text:
                continue
            # 部分命中的点只跟【冒号后的因果链】比（条件那半句允许写），
            # 否则照实写条件的答卷会被误判（见 MIN_COMMON 处的说明）。
            cmp_text = _causal_half(text) if key == "partial" else text
            need = _required_run(cmp_text, common_limit)
            ratio = similarity(cmp_text, answer)
            common = longest_common(cmp_text, answer)
            if ratio > limit or common >= need:
                why = (
                    f"与评分标准连续雷同 {common} 字（该点最长允许 {need} 字）"
                    if common >= need
                    else f"与评分标准重合度 {ratio:.0%}"
                )
                problems.append(f"第{seq}点要求「{required}」，{found}（{why}）")

    # ---- 核对 AI 有没有"说到做到" ----
    # 没写自述（flaw 为空）时无从核对，退回上面的纯文本比对，不报这一组问题。
    if not (flaw or "").strip():
        return problems

    declared = parse_declared(flaw)

    # ① 部分命中：判据不是"与标准答案像不像"，而是
    #    "自己声明要漏掉的那段因果，是不是真的没写"。
    #    条件写了、方向对，都是允许的——这正是"知道个大概、推不下去"该有的样子。
    for seq in plan.partial:
        text = points.get(seq)
        if not text:
            continue
        terms = declared.get(seq, [])
        if not terms:
            problems.append(
                f"第{seq}点是部分命中，自述里没有说明要用「」漏掉哪段因果"
                "（程序靠这个核对，必须原样摘抄采分点里的字）"
            )
            continue
        for term in terms:
            if not _in_point(term, text):
                problems.append(
                    f"第{seq}点自述里的「{term}」不是该采分点表述里的字，"
                    "请从采分点里原样摘一段要漏掉的因果"
                )
            elif _term_leaked(term, answer):
                problems.append(
                    f"第{seq}点自述里说要漏掉「{term}」，答案里却写了"
                )

    # ② 没抽到的点必须交代（整条不写）；未命中的点必须交代"写成什么样"
    for seq in list(plan.off) + list(plan.blank):
        if seq not in declared:
            problems.append(f"第{seq}点该怎么省、只写到什么程度，自述里没有说明")
    return problems


def answer_length_problem(question: Question, plan: StudentPlan, answer: str) -> str:
    """篇幅检查：只管"明显写太少"。

    实测踩过：AI 会把答案压成光秃秃的几个词（"3. 黄土。"）——
    那不是学生答案，是残句，教师一眼就能看出是机器生成的。
    所以这里用同一套篇幅口径兜住：字数不到该档下限一半，就打回重写。
    超长不判违规（中上水平本就写得多）。

    ⚠ 2026-09-29 起生成时不再给评分细则，"这位学生该写几条"已无从得知，
      改为按【题面满分 ÷ 每点分值】估算（expected_points）。
    ⚠ 2026-09-29 用户要求：五档篇幅已经拉平（见 planner.LENGTH_SPEC），
      差生也要写满答题卡。这里拦的不再是"差生写得短"，
      而是"任何一位学生被压成了残句"。全班之间的长短对比
      由 class_length_problem() 单独把关。
    """
    from .planner import expected_points, length_bounds

    point_score = question.points[0].score if question.points else 2.0
    n_expect = expected_points(question.max_score, point_score)
    lo_l, _hi_l, lo_c, _hi_c = length_bounds(plan.ability, n_expect)
    min_lines = max(1, min(lo_l, n_expect))
    n_points = count_points(answer)
    # 下限的一半（不少于 30 字）——低于这个就明显是残句，不是"写得简洁"
    min_chars = max(30, int(lo_c * 0.5))
    if len(answer) < min_chars:
        return (
            f"写得太少（只有 {n_points} 条、{len(answer)} 字），"
            f"至少要写到 {min_chars} 字，且每条都是完整通顺的句子"
        )
    if n_points < min_lines:
        return f"分点太少（只写得出 {n_points} 条，至少要 {min_lines} 条）"
    return ""


def class_length_problem(
    answers: Dict[int, str],
    min_ratio: float = 0.6,
    max_ratio: float = 1.6,
) -> Dict[int, str]:
    """全班篇幅均衡：不许出现"一位同学写满一页、另一位只写两行"。

    2026-09-29 用户要求。判据【不是】各档的绝对字数，而是这一班同学
    之间的相对差距——同一道题、同一个班，学生写在答题卡上的篇幅本来就
    该是接近的：好学生强在内容准、说得透，差学生弱在说不到点子上，
    而不是"一个写满一页、另一个只写两行"（那种卷子一眼就是假的）。

    基准取全班的【中位数】而不是平均数：
      · 一份写得特别长的答卷不该把标准整体抬高，逼着别人一起注水；
      · 两人小班时也不至于被极端值带飞。
    另外，算字数用的是去掉标点和数字之后的实义字数，
    免得有人靠多打标点、多抄数字把篇幅撑起来。

    返回 {学生序号: 问题描述}；空字典表示全班篇幅整齐。
    """
    lens: Dict[int, int] = {}
    for seq, text in (answers or {}).items():
        t = (text or "").strip()
        if t:
            lens[int(seq)] = len(_normalize(t))
    if len(lens) < 2:          # 只有一份时无从比较
        return {}
    vals = sorted(lens.values())
    n = len(vals)
    mid = float(vals[n // 2]) if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    if mid <= 0:
        return {}

    out: Dict[int, str] = {}
    for seq, ln in sorted(lens.items()):
        if ln < mid * float(min_ratio):
            out[seq] = (
                f"全班篇幅不整齐：这位学生只写了 {ln} 字，"
                f"同班其他同学大致在 {int(mid)} 字上下，他明显偏短"
            )
        elif ln > mid * float(max_ratio):
            out[seq] = (
                f"全班篇幅不整齐：这位学生写了 {ln} 字，"
                f"比同班同学（大致 {int(mid)} 字）明显偏长"
            )
    return out


# 分点编号："1." "2、" "3）" "4．" 都算。
# 前面的否定环视 + 后面不许紧跟数字，是为了别把"约3.2万个"里的"3."数进来。
_POINT_MARK = re.compile(r"(?<![0-9])([1-9]\d{0,1})\s*[.、)）．](?!\d)\s*")


def count_points(answer: str) -> int:
    """数这份答案写了几条（按"1. 2. 3."编号数，没有编号就按行数）。

    ⚠ 不能只按行数：AI 时不时会把几条挤在同一行里（"1. …2. …3. …"），
      按行数会被误判成"只写了一条"，白打回一轮改写、白花额度。
    """
    text = answer or ""
    marks = len(_POINT_MARK.findall(text))
    lines = len([x for x in text.splitlines() if x.strip()])
    return max(marks, lines)


def split_inline_points(answer: str) -> str:
    """把挤在一行里的分点拆成独立行（"1. …2. …" → 两行）。

    AI 偶尔会把整份答案写成一整行，教师看起来像一段话，不像答题卡。
    这一步纯字符串处理，不花钱，也不会改动任何文字内容。
    """
    out: List[str] = []
    for line in (answer or "").splitlines():
        if not line.strip():
            continue
        positions = [m.start() for m in _POINT_MARK.finditer(line)]
        if len(positions) < 2:          # 这行本来就只有一条，不动它
            out.append(line.strip())
            continue
        for i, pos in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(line)
            seg = line[pos:end].strip()
            if seg:
                out.append(seg)
    return "\n".join(out)


def clean_answer(answer: str) -> str:
    """清掉答案里不该出现的东西：分值标记、空条号、重复标点、断裂语病。

    这些大多是 AI 生成时的小毛病，但会直接暴露给教师看，
    所以在入库前统一清一遍，让答卷看起来像真的学生写的。
    """
    if not answer:
        return answer
    # AI 偶尔会把"（2分）""(2 分)"写进学生答案里
    text = re.sub(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]", "", answer)
    # 删掉整条后留下的空条号，并重新编号（"1. 2. 4." → "1. 2. 3."）
    lines = _renumber([x for x in text.splitlines() if x.strip()])
    text = "\n".join(lines)
    # 几条挤在同一行的，拆成独立行（AI 偶尔会这么写，看着不像答题卡）
    text = split_inline_points(text)
    # 断裂语病："该地区属于，气温较高" → "该地区气温较高"
    text = _BROKEN_LINK.sub("", text)
    # 重复标点："。。""，，" 压成一个
    text = re.sub(r"([。！？，、；：])\1+", r"\1", text)
    return text.strip()


def _strip_run(sentence: str, point_text: str, min_run: int) -> str:
    """删掉句子中与标准表述连续雷同的那一段。"""
    length, start, end = longest_common_span(point_text, sentence)
    if length < min_run or start < 0:
        return sentence
    _, idx = _normalize_with_map(sentence)
    raw_start = idx[start]
    raw_end = idx[end - 1] + 1
    out = sentence[:raw_start] + sentence[raw_end:]
    out = re.sub(r"[，、；：]{2,}", "，", out)
    out = re.sub(r"^[\s，、；：。]+", "", out)
    return out.strip()


# 删完片段后容易挂在句尾的"半截词"
_DANGLING = ("属于", "是", "的", "地", "了")
# "属于，""是，"这种"动词接逗号"就是把主语删掉后留下的断裂，
# 直接把连接词一起去掉，句子反而通顺。
# 前面加否定环视是为了别误伤"因为，""由于，"这类正常的连接词。
_BROKEN_LINK = re.compile(r"(?<![因由作认])(属于|是|的|地|了)\s*[，、]\s*")

# 最后一招：实在改不动（标准表述就写在句首，删哪一段都留残句），
# 就把这一条换成"绕圈子的空话"——真实考场里最常见的一种半吊子写法：
# 写了一句像回事的话，但什么信息都没给出。
# 多条轮换，避免全班答案撞成同一个句式。
FILLERS = (
    "该地区的地理条件对河流特征有重要影响。",
    "上述现象与当地的自然环境密切相关。",
    "流域内的水文特征受多种因素共同影响。",
    "自然环境方面的因素在这里起到了一定作用。",
    "这与该区域所处的位置和气候条件有关。",
    "该地所处的位置决定了它的一些特点。",
)


def _filler(salt: int) -> str:
    return FILLERS[abs(int(salt)) % len(FILLERS)]


# 被删除片段后剩下的"光杆介词/动词 + 两三个字"，如"受西南""因降水"，
# 读起来就是机器改过的痕迹，整条丢掉
_STUB_CLAUSE = re.compile(
    r"^(受|因|由|在|从|向|对|为|与|和|使|把|被|是)[^，。；！？]{0,3}$"
)


def _tidy(cand: str) -> str:
    """清理被删改后的碎片，避免留下"区地势低平""受西南，"这类残句。"""
    s = _BROKEN_LINK.sub("", (cand or "").strip())
    s = re.sub(r"[，。；、：]+$", "", s)
    s = re.sub(r"^[\s，、；：。]+", "", s)

    # 逐分句体检：太短的残句、光杆介词开头的半句，一律丢掉
    clauses = [c.strip() for c in re.split(r"[，,、；;]", s) if c.strip()]
    kept = [c for c in clauses if len(c) >= 3 and not _STUB_CLAUSE.match(c)]
    if clauses and not kept:
        return ""  # 整句都是碎片，交给下一级方案
    s = "，".join(kept)

    for _ in range(3):
        if len(s) > 3:
            for d in _DANGLING:
                if s.endswith(d) and len(s) - len(d) >= 4:
                    s = s[: -len(d)].rstrip("，、；：")
                    break
            else:
                break
        else:
            break
    return s.strip()


def _remove_terms(sentence: str, terms: List[str]) -> str:
    """把 AI 自述里声明"要漏掉"的词从句子里摘掉。

    词是原样写的最好；若被插了字（"季节分配【相对】均匀"），
    就把与该词重合最长的那一段切掉。
    """
    out = sentence
    for t in terms or []:
        if not t:
            continue
        if t in out:
            out = out.replace(t, "")
            continue
        length, _start, _end = longest_common_span(t, out)
        if length >= 3:
            out = _strip_run(out, t, 3)
    out = re.sub(r"[，、；：]{2,}", "，", out)
    # 摘掉词之后，句首或条号后面可能留下一个孤零零的逗号（"2. ，河流水量充足"），
    # 一并清掉，免得卷面上出现一眼就是机器改过的痕迹
    out = re.sub(r"^(\s*\d+\s*[.、)）．]\s*)[，、；：]+\s*", r"\1", out)
    out = re.sub(r"^[，、；：]+", "", out)
    return out


# 因果从句：真实考场里"只写结论不写理由"的答案，就是这样断的
_CAUSE_HEAD = re.compile(r"(因为|由于)[^，。；！？]{2,}[，,]")
_EFFECT_TAIL = re.compile(r"[，,](因此|因而|所以|导致|使得|从而|致使)[^，。；！？]{2,}")
# 句尾的"因为/由于"残根（删掉从句后留下的）
_CAUSE_DANGLING = re.compile(r"(因为|由于)[，,]?\s*$")


def _drop_reasoning(sentence: str) -> str:
    """删掉因果从句，只留结论——"只写结论，不写理由"的机械实现。"""
    out = _EFFECT_TAIL.sub("", _CAUSE_HEAD.sub("", sentence))
    out = _CAUSE_DANGLING.sub("", out)
    return out.strip()


def _degrade(
    sentence: str,
    point_text: str,
    min_run: int = 6,
    salt: int = 0,
    terms: List[str] | None = None,
    allow_filler: bool = True,
) -> str:
    """把一句"写成了满分"的话改残。

    逐级递进，取第一个"不再与标准表述雷同"的结果：
      ⓪ 摘掉 AI 自己声明"要漏掉"的关键词
      ① 删掉因果从句，只留结论（"只写结论、不写理由"）
      ② 删掉与标准表述雷同的片段（如"流域降水丰富径流量大，河流水量充足"
         → "河流水量充足"，标准表述那半句就没了）
      ③ 只保留第一个分句
      ④ 只保留前 8 个字（写到一半的样子）
      ⑤ 换成一句"绕圈子的空话"——标准表述就写在句首时，前几招都会留残句
    全都不行就返回空串，由调用方决定是否整句删掉。

    两个必须守住的地方：
      · 只从句子中间删。若雷同片段就在句首，删完会留下"区地势低平"这种
        断头句，宁可退到下一级方案。
      · 删改后一定要通顺。宁可整句删掉（当成"漏答"），也不要留一堆
        一眼就能看出是机器改出来的碎片。
    """
    body = sentence.strip()
    prefix = ""
    m = re.match(r"^(\s*\d+\s*[.、)）．]\s*)", body)
    if m:
        prefix, body = m.group(1), body[m.end() :]

    terms = [t for t in (terms or []) if t]
    cands: List[str] = []
    if terms:
        cands.append(_remove_terms(body, terms))
    cands.append(_drop_reasoning(body))
    length, start, _end = longest_common_span(point_text, body)
    if length >= min_run and start > 0:
        cands.append(_strip_run(body, point_text, min_run))
    cands.append(re.split(r"[，。；！？]", body)[0] if body else "")
    cands.append(_normalize(body)[:8])
    if allow_filler:
        cands.append(_filler(salt))

    for raw in cands:
        cand = _tidy(raw)
        if len(cand) < 4:
            continue
        text = prefix + cand + "。"
        if any(_term_leaked(t, text) for t in terms):
            continue  # 声明要漏掉的词还在（含"插两个字"的变相写法），这一级不算数
        if (
            longest_common(point_text, text) < min_run
            and similarity(point_text, text) <= 0.5
        ):
            return text
    return ""


def _renumber(lines: List[str]) -> List[str]:
    """删掉整条后重新编号，并清掉"2."这种空条。"""
    out: List[str] = []
    n = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^\s*(\d+)\s*[.、)）．]\s*(.*)$", s)
        if m:
            body = m.group(2).strip()
            if not body:
                continue
            n += 1
            out.append(f"{n}. {body}")
        else:
            out.append(s)
    return out


def force_fix(
    question: Question,
    plan: StudentPlan,
    answer: str,
    thresholds: Dict[str, float] | None = None,
    min_common: Dict[str, int] | None = None,
    flaw: str = "",
    forbidden: Optional[Dict[str, Iterable[str]]] = None,
) -> str:
    """本地兜底改写：AI 几轮都改不动时，直接动手删／改浅。

    ⚠ 已默认停用（config.yaml 的 classroom.force_fix 控制）。
    这条路会在卷面上留下机器痕迹，目前改用"让 AI 写浅一层"。

    处理三类问题：
      · "未作答"的点被写了 → 整条删掉
      · "草草了事"的点却写到了点子上 → 删掉那句（变成空答，同样是 0 分；
        本地没有学科知识去"写得更浅"，删比硬改安全）
      · "沾边"的点把因果链写全了 → 删掉与标准表述雷同的片段，改成半句
      · AI 自述里声明"要漏掉"的话仍出现在答案里 → 直接摘掉
    """
    th = dict(THRESHOLDS)
    if thresholds:
        th.update({k: float(v) for k, v in normalize_keys(thresholds).items()
                   if v is not None})
    common_limits = dict(MIN_COMMON)
    if min_common:
        common_limits.update({k: int(v) for k, v in normalize_keys(min_common).items()})

    points = {p.seq: p.text for p in question.points}
    if not points or not answer.strip():
        return answer
    declared = parse_declared(flaw)
    # AI 声明"要漏掉"的词，全局收集一份：只要出现在答案里就摘掉。
    # 不按"这句话最像哪个采分点"去定位——有些话跟标准表述的重合度不高，
    # 却明明白白写着它自己说要漏掉的那个词，按相似度定位反而会漏掉。
    leak_terms = [
        t
        for seq in list(plan.partial) + list(plan.blank)
        for t in declared.get(seq, [])
        if t
    ]

    kept: List[str] = []
    changed = False
    # 方向红线：本地没有学科知识去"把话改到正确方向上"，整句删掉最稳妥——
    # 删掉就是"漏答"，阅卷时同样是 0 分，不会把跑题的内容留在卷面上。
    bad_words = _forbidden_words_for(question, forbidden)
    # 空话一句就够了：同一份答案里出现两句，看起来就假了。
    # 第二处改不动时宁可整条删掉（当成漏答）。
    filler_used = False
    for line in answer.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # ⓪ 跑方向的句子整句删掉
        if any(w in line_stripped for w in bad_words):
            changed = True
            continue

        # ① 先把"说好要漏掉却写了"的词摘掉
        leaked = [t for t in leak_terms if _term_leaked(t, line_stripped)]
        if leaked:
            line_stripped = _tidy(_remove_terms(line_stripped, leaked))
            changed = True
            if len(line_stripped) < 4:
                continue  # 摘完没剩什么，整条删掉

        # ② 找出这一句最可能对应哪个采分点
        best_seq, best_ratio = None, 0.0
        for seq, text in points.items():
            ratio = similarity(text, line_stripped)
            if ratio > best_ratio:
                best_seq, best_ratio = seq, ratio

        # 门槛定得低（0.15）：定位句子的归属而已，判得严不严由后面的
        # "重合度/连续雷同"决定，宁可多看一眼也别漏掉该偏开的句子。
        if best_seq is not None and best_ratio > 0.15:
            text = points[best_seq]
            if best_seq in plan.blank:
                changed = True
                continue  # 整条删掉
            if best_seq in plan.off:
                # 本该答非所问却答到了点上：本地没有学科知识"换个方向写"，
                # 删掉这句最稳妥——变成空答，阅卷时同样是 0 分，符合设计。
                need = _required_run(text, common_limits.get("off", 6))
                if (
                    best_ratio > th.get("off", 0.45)
                    or longest_common(text, line_stripped) >= need
                ):
                    changed = True
                    continue
            if best_seq in plan.partial:
                limit = th.get("partial", 0.75)
                run = common_limits.get("partial", 7)
                need = _required_run(text, run)
                leak = [
                    t
                    for t in declared.get(best_seq, [])
                    if _term_leaked(t, line_stripped)
                ]
                if (
                    best_ratio > limit
                    or longest_common(text, line_stripped) >= need
                    or leak
                ):
                    fixed = _degrade(
                        line_stripped,
                        text,
                        run,
                        salt=plan.seq * 31 + best_seq,
                        terms=leak,
                        allow_filler=not filler_used,
                    )
                    changed = True
                    if not fixed:
                        continue  # 改不动就整句删掉（当成漏答）
                    if any(f.rstrip("。") in fixed for f in FILLERS):
                        filler_used = True
                    line_stripped = fixed
        kept.append(line_stripped)

    if not changed or not kept:
        # 兜底的底线：宁可原样保留，也不能把答卷清空
        return answer
    return "\n".join(_renumber(kept))
