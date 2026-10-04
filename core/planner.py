"""学生水平分档：决定每份答卷"答哪几个点、每个点答到什么程度"。

注意：这里算出来的 design_score 只是生成答案时的参考值，
不参与最终判分，也不会在界面上显示。真正的分数由 AI 阅卷给出。

═══════════════════════════════════════════
核心逻辑（用户 2026-09-26 定的三段式，务必按这个来）
═══════════════════════════════════════════
【第一步 · 抽点】
评分细则是一个"池子"（比如 6 分的题给了 5 个采分点）。
代码先随机抽 3~4 个点，作为这个学生的【答题范围】——
**严禁把池子里的点全给他**。没抽到的点他整条不写。
这一步是关键：光是"质量分配"按不住分，学生把抽到的点全写对照样满分；
先限定了"他只答这几条"，正确率才真的压得住。

【第二步 · 每个点分配质量档位】
对抽出来的那几个点，再逐个分配三档质量：
    完整命中（2 分）：条件 + 因果机制 + 落脚结论，三环齐全、术语准确
    部分命中（1 分）：只说了一半——写到了"条件/现象"，
                      但采分点要的因果机制没写出来
    未命中   （0 分）：方向偏了，或只丢一句结论没有论据，拿不到分
举例：抽了 3 个点，质量分配为"完整 + 部分 + 未命中" → 2+1+0 = 3 分。
这才是真实的中等生：不是每点都差一口气，而是"对一条、半对一条、废一条"。

【第三步 · 总分封顶】
最终得分 = min(题目满分, 各点得分之和)。候补采分点全答对也不超满分。

【红线】
· 不许写【低级错误】（不许写"海拔越高气温越高""亚热带写成温带"）。
· 不许"跑方向"（问自然不许写经济/人口，问有利不许写制约/崎岖）——
  未命中指的是"这一条没答到点子上"，不是"答到别的话题上去"。
· 【似是而非】是唯一允许的"错"：条件写对，但因果机制张冠李戴、
  机制错位或因果倒置——听着头头是道、内行一看就错。
  这是真实考生最常见的失分方式之一（2026-09-29 用户要求），与"写浅"并列为主力。
"""

from __future__ import annotations

import logging
import random
from typing import Dict, List, Optional, Tuple

from .models import Question, StudentPlan

log = logging.getLogger(__name__)

# 五类"半吊子"画像（写给 AI 看的错误类型标签）
# 前两类是主力：①方向对、条件也写了，但因果链推不下去；
#              ②条件对，但因果机制"似是而非"——听着有道理，实际是错的。
HALF_STYLES = ("浮于表面型", "似是而非型", "只报结论型", "术语笼统型", "半途而止型")
# 未命中档只会从这两种里抽（很差的学生连一句完整的话都写不完）
LOW_STYLES = ("浮于表面型", "半途而止型")
SOLID_STYLE = "扎实型"

# 部分命中该点能拿多少（= 该点分值的 1／2）。
# 用户定的三档是"完整 2 分 / 部分 1 分 / 未命中 0 分"，所以这里是 0.5。
# ⚠ 改这个数就必须同步改 TARGET_RATIO，否则各档会跑到别的组合上去。
PARTIAL_CREDIT = 0.5

# ---------------------------------------------------------------------------
# 逐点下发要求：告诉 AI 这个点"该写成什么样"
# ---------------------------------------------------------------------------

# 完整命中（2 分档）
HINT_HIT = (
    "完整命中：条件／现象 + 因果机制 + 落脚结论三环都写全，术语准确。"
    "可以写得规范，但要用学生自己的话组织，严禁照抄评分细则原句"
)

# 部分命中（1 分档）——主力。
# 共同点：条件写到了（不离题），句子完整通顺，但因果链没写出来。
PARTIAL_SURFACE = (
    "部分命中【浮于表面型】：用完整的一句话写清条件和现象，"
    "后面接一句笼统的好处（如'昼夜温差大，有利于青稞生长'），"
    "但采分点要的因果机制不写"
)
PARTIAL_CONCLUSION = (
    "部分命中【只报结论型】：写到本题要的那个结论／作用就停，"
    "中间'为什么'那一步整段不写"
    "（如只写'昼夜温差大，有利于有机物质积累'，不写光合、呼吸作用）"
)
PARTIAL_TERM = (
    "部分命中【术语笼统型】：通篇用大白话描述，一个专业术语都不出现"
    "（如把'光合作用强'写成'光照条件比较好'）"
)
PARTIAL_HALF = (
    "部分命中【半途而止型】：写出条件和中间一个环节就收笔，链条差最后一截"
    "（如只写'海拔较高，气温偏低，作物生长会受影响'）"
)
PARTIAL_MISLEADING = (
    "部分命中【似是而非型】：条件／现象本身写对了，但因果机制是错的——"
    "要写得【听着头头是道、内行一看就错】，三种手法任选："
    "① 张冠李戴：把别处才成立的机制套到这里（如把降水少归因于'纬度较高'）；"
    "② 因果倒置：把结果当成原因（如说'因为作物长得好，所以昼夜温差大'）；"
    "③ 泛化套路：背一句万能原因硬套（如什么都答'位于板块交界处，地壳活跃'）。"
    "⚠ 红线：只许错在机制，条件不能写错；"
    "严禁低级常识错误（不许写'海拔越高气温越高''亚热带写成温带'）；"
    "用词保持中性书面语，不许出现跑方向的词"
)

PARTIAL_HINTS = (
    PARTIAL_SURFACE, PARTIAL_MISLEADING, PARTIAL_CONCLUSION,
    PARTIAL_TERM, PARTIAL_HALF,
)

# 未命中（0 分档）
# ⚠ 是"这一条没答到点子上"，不是"答到别的方向去"——方向红线不许跑题。
OFF_CONCLUSION_ONLY = (
    "未命中：只丢一句笼统的结论，既不写条件也不写论据，"
    "落不到这个采分点上（如只写'对当地农业生产有帮助'）"
)
OFF_OFF_TARGET = (
    "未命中：写的内容跟这个采分点对不上（但仍要咬住题目问的方向，"
    "不许跑到别的要素上去），明显是在凑字数"
)
OFF_HINTS = (
    OFF_CONCLUSION_ONLY,
    OFF_OFF_TARGET,
)

# 画像决定"这份答案主要缺在哪儿"，会让同一类缺陷贯穿全卷。
# 取值必须落在 PARTIAL_HINTS 里（测试会核对这一点）。
STYLE_PARTIAL_HINT: Dict[str, str] = {
    "浮于表面型": PARTIAL_SURFACE,
    "似是而非型": PARTIAL_MISLEADING,
    "只报结论型": PARTIAL_CONCLUSION,
    "术语笼统型": PARTIAL_TERM,
    "半途而止型": PARTIAL_HALF,
}

# 没抽到的点：整条不写
HINT_BLANK = "整条不写：他没有答到这一条，答案里完全不出现相关内容"

# 各档位的"目标得分率"（相对题面满分）。
# 中等档定 0.50 的依据是用户举的例子：6 分的题抽 3 个点，
# "完整 + 部分 + 未命中" = 2+1+0 = 3 分，正好一半。
TARGET_RATIO: Dict[str, float] = {
    "优秀": 0.90,
    "良好": 0.70,
    "中等": 0.50,
    "薄弱": 0.30,
    "很差": 0.10,
}

# 篇幅约束：{档位: (最少分点条数, 最多分点条数, 最少字数, 最多字数)}
# 字数按"4 条分点"为基准，实际生成时会随【该生抽到的点数】缩放。
#
# 设计原则（2026-09-29 用户最终确认的版本）：
#   · 中上三档（优秀／良好／中等）**写满**——真实考场里中等生、良好生
#     在答题纸上写得并不比尖子生少，他们的问题是"写了很多却没答到点上"。
#   · 下面两档（薄弱／很差）**明显写得少**，因为这是区分度的命根子。
#     ⚠ 实测教训（第 30 号 vs 第 23 号）：
#       把很差的篇幅也拉到 145~205 字之后，AI 扮演"基础很差的学生"
#       并不会真的变笨，它只是把话说得笼统些——内容上照样答对，
#       于是"很差"档写了 194 字、AI 判了 7 分（满分 8），和中等档分不出高下。
#       压回 60~110 字后（对照第 23 号：很差档 43 字 → AI 判 0 分），区分度才回来。
#   · 但也不是"交白卷"：很差档仍要求 2 条以上、60 字以上，
#     比"只写一两个词"体面，只是说得少、说得浅。
LENGTH_SPEC: Dict[str, Tuple[int, int, int, int]] = {
    "优秀": (4, 6, 165, 235),
    "良好": (4, 6, 160, 228),
    "中等": (4, 6, 155, 220),
    "薄弱": (3, 5, 100, 155),
    "很差": (2, 4, 60, 110),
}


def length_bounds(ability: str, n_points: int = 4) -> Tuple[int, int, int, int]:
    """按档位和【该生要写几条】算出篇幅上下限。

    采分点多的大题自然该写长一些；这里传进来的是他抽到的点数，
    不是池子里的点数——"他只答 3 条"就该按 3 条的篇幅写。

    这个函数被两处共用：生成时给 AI 的篇幅要求、生成后的篇幅检查。
    ⚠ 不能让两边各算一份，否则会出现"提示词说 180 字、检查却按 145 字"
      这种自相矛盾的情况。
    """
    lo_l, hi_l, lo_c, hi_c = LENGTH_SPEC.get(ability, (4, 6, 155, 220))
    n = max(1, int(n_points or 4))
    # 缩放系数收窄在 0.7~1.15：采分点多的大题确实该写长一点，
    # 但篇幅不能被题分放大失控——五档要一起长、一起短，保持"全班篇幅整齐"。
    k = max(0.7, min(1.15, n / 4))
    lo_c2 = int(round(lo_c * k / 5)) * 5
    hi_c2 = int(round(hi_c * k / 5)) * 5
    # 分点条数不超过"要写的点数 + 2"，避免小题硬凑条数
    hi_l2 = max(lo_l, min(hi_l, n + 2))
    lo_l2 = min(lo_l, hi_l2)
    return lo_l2, hi_l2, lo_c2, hi_c2


def length_hint(ability: str, n_points: int = 4) -> str:
    """生成给 AI 看的篇幅要求。"""
    lo_l2, hi_l2, lo_c2, hi_c2 = length_bounds(ability, n_points)
    return f"{lo_l2}~{hi_l2} 条分点，总字数 {lo_c2}~{hi_c2} 字"


DEFAULT_STYLE_WEIGHTS: Dict[str, float] = {
    "浮于表面型": 0.35,
    "似是而非型": 0.25,
    "只报结论型": 0.15,
    "术语笼统型": 0.10,
    "半途而止型": 0.15,
}


# ---------------------------------------------------------------------------
# 角色提示词（2026-09-29 用户要求 · 生成学生答案的唯一控制手段）
# ---------------------------------------------------------------------------
# 生成答卷时【不再把评分细则发给 AI】，所以"哪一条写残"这种逐点指令
# 已经下不了了。学生的水平差异改为靠【角色提示词】控制：
# 让 AI 扮演一个特定水平的高中生，只凭自己的知识储备和理解作答。
# 它不知道采分点是什么，自然会遗漏、偏差、不精确——这才是真实考场的样子。
ROLE_PROMPTS: Dict[str, str] = {
    "优秀": "你是一名地理基础扎实、平时考试能拿高分的高中生。"
            "认真审题，把你能想到的地理过程、因果关系写清楚，尽量答完整。",
    "良好": "你是一名地理成绩中上、偶尔会丢细节的高中生。"
            "按自己的理解作答，主要方向能写对，但不必追求面面俱到。",
    # ⚠ 【2026-10-02 用户要求压平均分】中等／薄弱两档要"会写，但不推因果"：
    #   把现象和条件一条条罗列出来（篇幅不减），却很少往后解释"所以会怎么样"。
    #   实测教训：改之前"中等生"能把 5 个采分点全覆盖、条条都有因果，
    #   实际拿到 7~8 分（满分 8），跟优秀生分不出高下，全班平均分虚高到 75%。
    #   现在的写法落到阅卷端就是"只写到核心采分点、没跟设问挂钩" = 1 分档。
    "中等": "你是一名地理成绩中等的学生。你答题的习惯是：把想起来的现象和条件"
            "一条条写出来（「降水集中」「森林覆盖率下降」「地势平坦」），"
            "但很少再往后推一步说「所以会怎么样」——答案读起来像在罗列现象，"
            "不像在讲道理。想到多少写多少，不用追求完美。",
    "薄弱": "你是一名地理基础较弱的学生。你只记得几个零散的说法，写出来多是"
            "一两个词或半句话（「降水集中」「森林减少」），后面接不上"
            "「所以会怎么样」。但不要把题空着——能想起多少就写多少，"
            "想不起来了就停笔。",
    "很差": "你是一名地理基础很差的学生。这次考试你几乎没复习，"
            "只写得出很少的几句话，说不出什么道理；但也不要把卷子空着——"
            "能想起多少就写多少，想不起来了就停笔。",
}

# 画像 → 答题习惯（附加在角色提示词后面，让同一水平的答案也有个体差异）
# ⚠ 每一条都只描述"内容上的毛病"，不描述篇幅——
#   篇幅由 LENGTH_SPEC 统一管住（中上三档写满，薄弱与很差明显写得少）。
STYLE_TRAITS: Dict[str, str] = {
    "扎实型": "答题有条理，术语用得准，写之前会先想清楚。",
    "浮于表面型": "写答案像在罗列关键词：把材料里的现象、名词一条条摆出来，"
                  "但几乎不往下解释「为什么会这样」「这对它有什么用」。",
    "似是而非型": "背过一些答题模式，喜欢直接往题目上套；"
                  "有时套得不太对（张冠李戴、因果说反），自己也察觉不到。",
    "只报结论型": "喜欢一条接一条地抛结论，依据和过程懒得写；"
                  "每条都干巴巴的、缺论据。",
    "术语笼统型": "课本上的专业说法记不牢，习惯用自己的大白话讲，"
                  "通篇没有一个专业术语。",
    "半途而止型": "每条都只说到一半就转去写别的，"
                  "前面那截道理没讲完；一条也没说透。",
}


def student_role(ability: str, style: str = "") -> str:
    """把档位 + 画像翻译成给 AI 的角色提示词（生成答卷时唯一能给的"控制信号"）。"""
    role = ROLE_PROMPTS.get(ability, ROLE_PROMPTS["中等"])
    trait = STYLE_TRAITS.get(style, "")
    if not trait:
        return role
    return f"{role} 你的答题习惯（{style}）：{trait}"


def expected_points(max_score: float, point_score: float = 2.0) -> int:
    """按题面满分估算"要想拿满分大概得答到几个点"（只用来算篇幅）。"""
    if max_score and max_score > 0 and point_score > 0:
        return max(1, int(round(float(max_score) / float(point_score))))
    return 4


def expected_score(ability: str, max_score: float) -> float:
    """这份答卷"大概能得多少分"的预期值（给报告当教师分的参照）。

    ⚠ 生成时不给细则之后，已经无法逐点推算了，只能用档位目标得分率估一个。
      真实分数一律以 AI 阅卷为准（阅卷仍拿完整细则逐点判）。
    """
    return round(min(float(max_score), float(max_score) * TARGET_RATIO.get(ability, 0.5)), 1)


def pick_ability(weights: Dict[str, float]) -> str:
    """按配置的人数占比，随机抽一个水平档位。"""
    r = random.random()
    acc = 0.0
    for name, w in weights.items():
        acc += float(w)
        if r < acc:
            return name
    return list(weights.keys())[-1]


def pick_style(
    ability: str, style_weights: Dict[str, float] | None = None
) -> str:
    """决定这份答卷的"错误画像"。

    优秀档固定扎实型；很差档从浮于表面/半途而止里抽；
    其余三档按 style_weights 抽四类半吊子画像。
    """
    if ability == "优秀":
        return SOLID_STYLE
    if ability == "很差":
        return random.choice(LOW_STYLES)

    weights = style_weights or DEFAULT_STYLE_WEIGHTS
    pool = {k: float(v) for k, v in weights.items() if k in HALF_STYLES}
    # 【防呆】配置里的画像名写错/用了旧名字时，以前是静默丢掉、配置白填，
    # 而"半对卷该长什么样"直接决定产品效果，不能悄悄失效——这里必须吼一声。
    dropped = [k for k in weights if k not in HALF_STYLES]
    if dropped:
        log.warning(
            "style_weights 里有程序不认识的画像名，已忽略：%s（合法名字只有：%s）",
            "、".join(dropped),
            "、".join(HALF_STYLES),
        )
    if not pool:
        log.warning("style_weights 没有一个合法画像名，改用默认比例：%s",
                    DEFAULT_STYLE_WEIGHTS)
        pool = dict(DEFAULT_STYLE_WEIGHTS)
    r = random.random()
    acc = 0.0
    for name, w in pool.items():
        acc += w
        if r < acc:
            return name
    return list(pool.keys())[-1]


def draw_range(n: int, max_score: Optional[float], mean_point: float) -> Tuple[int, int]:
    """第一步：算出这个学生"答几个点"的范围。

    规则：以"凑满满分所需的点数"为基准（6 分 ÷ 2 分 = 3 个点），
    上下各留一点余量（3~4 个），并且**池子里的点不许全给他**。
    """
    need = n
    if max_score and float(max_score) > 0 and mean_point > 0:
        need = int(round(float(max_score) / mean_point))
    need = max(1, min(need, n))

    k_lo = need
    k_hi = min(n, need + 1)
    # 池子比需要的大（有候补点）→ 最多给到 n-1，绝不全给
    if n > need and k_hi >= n:
        k_hi = max(k_lo, n - 1)
    k_hi = max(k_lo, k_hi)
    return max(1, min(k_lo, n)), max(1, min(k_hi, n))


def _blank_count(k: int, ability: str, blank_rate: float) -> int:
    """在抽到的点里，再额外整条漏答几条（模拟"想到了但没写"）。"""
    if k <= 1 or blank_rate <= 0:
        return 0
    factor = {"很差": 3.0, "薄弱": 2.0, "中等": 0.5}.get(ability, 0.0)
    if factor <= 0:
        return 0
    return 1 if random.random() < min(blank_rate * factor, 0.9) else 0


def split_points(
    n: int,
    ability: str,
    blank_rate: float = 0.0,
    point_scores: Optional[List[float]] = None,
    max_score: Optional[float] = None,
) -> Tuple[List[int], List[int], List[int], List[int]]:
    """三步走，返回 (完整命中, 部分命中, 未命中, 没抽到)。

    ① 抽点：从 n 个采分点里随机抽 k 个当"答题范围"（k 由 draw_range 定）；
       没抽到的一律整条不写。
    ② 分配质量档位：枚举"几条完整 + 几条部分 + 几条未命中"的所有组合，
       挑最接近该档目标分的那个。
    ③ 组合得分已经按满分封顶过，调用方再封一次即可（见 make_plans）。

    挑组合时有两级形状优先（分数一样时决定"卷面长什么样"）：
      · 先用到的档位种类越多越像真人——中等生是"对一条、半对一条、废一条"，
        不是"每条都半对"。用户明确点过这一点。
      · 再优先"部分命中"多的（半吊子是这个产品的主角）。
    """
    if n <= 0:
        return [], [], [], []

    scores = [float(s) for s in (point_scores or [2.0] * n)]
    if len(scores) != n:
        scores = [2.0] * n

    # 打乱后按分值从大到小排（同分时沿用打乱顺序，sort 是稳定的）
    all_seqs = list(range(1, n + 1))
    random.shuffle(all_seqs)
    ranked_seqs = sorted(all_seqs, key=lambda s: -scores[s - 1])
    ranked = [scores[s - 1] for s in ranked_seqs]

    total = sum(ranked)
    # 参照上限：题面满分（候补采分点不该变成额外的得分机会）
    cap = float(max_score) if max_score and float(max_score) > 0 else total
    cap = min(cap, total) if total else cap
    target = cap * TARGET_RATIO.get(ability, 0.30)
    # 轻微抖动：避免同一档位的每份答案都写成同一个样子
    target += random.uniform(-0.15, 0.15)

    # ---- ① 抽点 ----
    mean_point = (sum(ranked) / len(ranked)) if ranked else 2.0
    k_lo, k_hi = draw_range(n, max_score or total, mean_point)

    # ---- ② 枚举"几条完整 + 几条部分"的所有组合 ----
    combos: List[Tuple[float, int, int, int, int]] = []
    for k in range(k_lo, k_hi + 1):
        for h in range(k + 1):
            for p in range(k - h + 1):
                raw = sum(ranked[:h]) + sum(ranked[h : h + p]) * PARTIAL_CREDIT
                got = min(raw, cap)          # 多答不加分
                miss = k - h - p
                kinds = len([1 for c in (h, p, miss) if c > 0])
                combos.append((abs(got - target), k, h, p, kinds))

    best_gap = min(c[0] for c in combos)
    # 容差 0.3 分：够在"分数几乎一样"的组合里挑形状，又不会抹平档次差距
    pool = [c for c in combos if c[0] <= best_gap + 0.3]
    # 形状优先 ①：档位种类覆盖得越全越像真人（完整 + 部分 + 未命中）
    best_kinds = max(c[4] for c in pool)
    pool = [c for c in pool if c[4] == best_kinds]
    # 形状优先 ②：部分命中越多越好（半吊子是主角）
    max_p = max(c[3] for c in pool)
    pool = [c for c in pool if c[3] == max_p]
    _gap, k, h, p, _kinds = random.choice(pool)

    drawn = ranked_seqs[:k]
    hit = drawn[:h]
    partial = drawn[h : h + p]
    off = drawn[h + p :]
    # 没抽到的点：不属于这个学生的答题范围，整条不写
    blank = ranked_seqs[k:]

    # ---- 额外漏答：从"未命中"里再挑几条整条不写（未命中本来也是 0 分）----
    n_blank = _blank_count(k, ability, blank_rate)
    if n_blank:
        for _ in range(min(n_blank, len(off))):
            blank.append(off.pop())
        if n_blank > len(blank) and partial:
            blank.append(partial.pop())

    return hit, partial, off, blank


def make_plans(
    question: Question,
    count: int,
    weights: Dict[str, float],
    blank_rate: float = 0.0,
    style_weights: Dict[str, float] | None = None,
) -> List[StudentPlan]:
    """为一整批学生生成"设计档位"和"错误画像"。"""
    scores = {p.seq: p.score for p in question.points}
    n = len(question.points)
    score_list = [p.score for p in question.points]
    plans: List[StudentPlan] = []

    for i in range(count):
        ability = pick_ability(weights)
        style = pick_style(ability, style_weights)
        hit, partial, off, blank = split_points(
            n, ability, blank_rate, score_list, question.max_score
        )
        # ③ 设计分：生成时不再给细则之后，逐点推不出分数了，
        #    只能按档位目标得分率估一个"预期分"，给报告当参照。
        #    真实分数一律以 AI 阅卷为准（阅卷仍拿完整细则逐点判）。
        design = expected_score(ability, float(question.max_score))

        # 逐点下发要求，防止 AI 自作主张把该写浅的点写到位
        part_fixed = STYLE_PARTIAL_HINT.get(style)
        defects: Dict[int, str] = {}
        for s in hit:
            defects[s] = HINT_HIT
        for s in partial:
            defects[s] = part_fixed or random.choice(PARTIAL_HINTS)
        for s in off:
            defects[s] = random.choice(OFF_HINTS)
        for s in blank:
            defects[s] = HINT_BLANK

        plans.append(
            StudentPlan(
                seq=i + 1,
                ability=ability,
                style=style,
                hit=hit,
                partial=partial,
                off=off,
                blank=blank,
                design_score=design,
                defects=defects,
            )
        )
    return plans
