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
# LENGTH_SPEC: Dict[str, Tuple[int, int, int, int]] = {
#     "优秀": (4, 6, 165, 235),
#     "良好": (4, 6, 160, 228),
#     "中等": (4, 6, 155, 220),
#     "薄弱": (3, 5, 100, 155),
#     "很差": (2, 4, 60, 110),
# }
#
# ⚠ 【2026-10-05 用户定：得分率必须落在 30~70%】改法见下面的版本。
#   改判分规则（grading_system）放宽了"缺因果连接"的扣分之后，
#   **生成端原先压平均分的那根杠杆作废了**——旧版 ROLE_PROMPTS 让中等生
#   "只罗列现象不推因果"，阅卷端据此给 1 分档；现在阅卷端只看
#   "核心采分点写出来没有"，罗列现象就是满分，中等生直接顶到 100%。
#   实测（第 53 份）：中等 8/8、很差 8/8，全班得分率 100%。
#
#   ★ 最终采用的设计：**条数一律放开，靠"故意写错"压分**。
#     用户明确要求"所有档位都写满，差生也写 4~5 条，跟优等生一样多"。
#     理由不只是"像真的"——砍条数确实能压分，但那样差生的卷面一眼
#     就能看出比优等生短，练批改时教师会先注意到长短而不是内容对错。
#     真实考场上把卷子写坏的主要方式本来就不是"写短"，而是【写错】。
#     具体三类错误 + 各档配额见下面的 WRONG_QUOTA。
#   ⚠ 配套改动（漏了会白烧额度）：
#     · prompts/grading_system.txt 恢复三档（满分/1分/0分），
#       但 1 分的触发条件明确成"采分点要分析、学生只写了现象"
#     · core/quality.py 的方向红线要读 WRONG_QUOTA，放行"故意的答非所问"
LENGTH_SPEC: Dict[str, Tuple[int, int, int, int]] = {
    "优秀": (4, 6, 165, 235),
    "良好": (4, 6, 160, 228),
    "中等": (4, 6, 155, 220),
    # ↓ 下两档从(3,5)/(2,4) 放宽到 (4,5)：用户要求差生也写满
    "薄弱": (4, 5, 120, 175),
    "很差": (4, 5, 110, 165),
}

# ---------------------------------------------------------------------------
# ★ 故意写错的配额（2026-10-05 用户设计 · 压得分率的主力阀门）
# ---------------------------------------------------------------------------
# 【为什么需要它】第 53 份实测：中等 8/8、很差 8/8，全班得分率 100%。
# 判分端没错——那两份确实命中了全部采分点。错在生成端：
# AI 扮演"基础很差的学生"时，写出来的东西依然是对的，只是话说得笼统；
# 而阅卷端放宽因果连接之后**只看核心采分点有没有命中**，
# 于是"写得笼统"完全不扣分——原先那根杠杆作废了。
#
# 【三类错误 · 真实考生最常见的失分方式】
#   ① 答非所问：句子本身地理知识正确，但讨论的不是本题问的方向
#   ② 因果颠倒：把结果当成原因，因果说反了
#   ③ 张冠李戴：把别处才成立的机制套到这里，听着对、其实错
# 教师在批改训练里最需要练的恰恰是识别这三类——它们"看起来像对的"，
# 是 AI 判分最容易放过、也最容易误导学生的一类失分。
#
# 结构：{档位: (最少错误条数, 最多错误条数)}
# ⚠ 优秀档固定 0 条——尖子卷不该有硬错。
# ⚠ 这张表只对【生成端】生效；判分端完全不知道它存在，
#   AI 阅卷官看到的就是一份"有错但看不出错在哪"的卷子。
WRONG_QUOTA: Dict[str, Tuple[int, int]] = {
    "优秀": (0, 0),
    "良好": (0, 1),
    #⚠ 中等为什么是 (1, 2) 而不是 (1, 1)：
    #   实测（第 53 份，8 分题）中等档只错1 条时得分率 100%——
    #   这道题"答出 4 个点即满分"，1 条错误只压 2 分（8→6 = 75%），
    #   而且 AI 常常"忘记改"某一条，实际生效的更少。
    #   抽 1~2 条能覆盖"真的写了 1 条"和"写了 2 条"两种情况，
    #   落在 62.5%~75% 这个更稳的带里。
    "中等": (1, 2),
    "薄弱": (2, 3),
    "很差": (2, 3),
}

# 三类错误的名称，必须与 prompts/answer_system.txt 里写的一致（一字不差）。
# test_core 有一条断言守着这个一致性——两边不同名，AI 收到自相矛盾的指令。
WRONG_KINDS = ("答非所问", "因果颠倒", "张冠李戴")

# ---------------------------------------------------------------------------
# ★★ 学科领域锁定（2026-10-05 用户第 66 批实测反馈）★★
# ---------------------------------------------------------------------------
# 【问题】人文题里冒出了"亚热带季风气候""板块交界处""地形崎岖"这种自然地理内容。
#   教师一眼看出"这不像差生写的，像根本没看题"——差生也会围绕题目问的方向瞎答，
#   不会把整道题串到另一个学科领域去。
#
# 【根因】原 WRONG_SAMPLES 里的示例**全是自然地理**（气候、地形、板块），
#   而错误示例是全局共用的——题目是人文题时，AI 照着自然地理的样例去套，
#   就把自然名词搬进来了。不是 AI 不听话，是**提示词本身给错了方向**。
#
# 【规矩】注入的错误必须留在题目的学科领域内：
#   · 人文题 → 错误只能是人文领域内的话题偏移（市场↔劳动力↔交通↔政策…）
#   · 自然题 → 错误只能是自然领域内的话题偏移（气候↔地形↔水文↔土壤…）
#   · 只有【很差】档，且只有【一部分人】，才允许出现跨领域名词
#     （模拟"完全没复习、连题目问哪一科都没抓住"的极端情况）
#
# ⚠ 判不出来时一律返回 ""（不锁定），宁可宽松也不误伤——
#   一旦把人文题误判成自然题，会强迫 AI 写自然名词，错得更离谱。

# 人文地理的典型主题词 / 题面词
_HUMAN_KEYS = (
    "人文", "产业", "区位", "城市", "人口", "农业", "工业", "服务业",
    "商业", "贸易", "电商", "旅游", "交通", "市场", "经济", "区域发展",
    "城镇化", "城市化", "环境问题", "可持续发展", "资源利用",
)
# 自然地理的典型主题词 / 题面词
_PHYSICAL_KEYS = (
    "自然", "地貌", "岩石", "地质", "构造", "气候", "天气", "水文",
    "河流", "水系", "洋流", "植被", "土壤", "地形", "盐风化", "风化",
    "侵蚀", "堆积", "板块", "大气", "降水", "温度", "生态",
)

# 分领域的错误改写示例。key = "人文" / "自然"，value = {错误类型: 示例}
WRONG_SAMPLES_BY_FIELD: Dict[str, Dict[str, str]] = {
    "人文": {
        "答非所问": (
            "在**人文领域内部**换个不相干的角度。"
            "例如问「市场因素」，改去写「当地劳动力资源丰富、素质较高」"
            "——还是人文因素，但答的不是设问要的那一条。"
            "或者问「交通条件」，改去写「该地政策扶持力度大」"
            "——同为人文因素，方向不对。"
        ),
        "因果颠倒": (
            "把人文因素之间的因果说反。"
            "例如本来该写「电商企业集聚，所以物流业发达」，"
            "改写成「物流业发达，所以电商企业集聚」"
            "（两者互为因果，但本题问的是前一个方向）；"
            "或者本来该写「产业集聚，所以地价上涨」，"
            "改写成「地价上涨，所以产业集聚」。"
            "⚠ 只在人文因素之间颠倒，不要牵扯别的领域。"
        ),
        "张冠李戴": (
            "在**人文领域内部**换一个别处才成立的机制。"
            "例如问「市场因素」，写成「因为当地劳动力充足，所以市场广阔」"
            "——把劳动力因素当成市场因素的成因，听着像回事，其实错位；"
            "或者问「交通区位」，写成「因为该地政策优惠，所以交通便利」"
            "——政策与交通硬接因果。"
            "⚠ 关键：引用的因素**本身必须是人文地理里真实存在的**，"
            "只是张冠李戴地接错了对象。"
        ),
    },
    "自然": {
        "答非所问": (
            "在**自然地理领域内部**换个不相干的角度。"
            "例如问「降水」，改去写「该地地形崎岖、土壤贫瘠」"
            "——也是自然因素，但答的不是设问要的那一条；"
            "或者问「岩石性质」，改去写「该地植被覆盖率高」"
            "——同属自然，方向不对。"
        ),
        "因果颠倒": (
            "把自然因素之间的因果说反。"
            "例如本来该写「昼夜温差大，有利于有机物积累」，"
            "改写成「有机物积累多，所以昼夜温差大」；"
            "或者本来该写「降水少，所以植被稀疏」，"
            "改写成「植被稀疏，所以降水少」。"
            "⚠ 只在自然因素之间颠倒，不要牵扯别的领域。"
        ),
        "张冠李戴": (
            "在**自然领域内部**换一个别处才成立的机制。"
            "例如讲流水作用时，套用「该地位于板块交界处，地壳活跃，"
            "所以流水侵蚀强烈」——把内力作用套到外力作用上；"
            "或者讲盐风化时，写成「因为该地降水丰富，所以盐分结晶膨胀」"
            "——把降水与盐分结晶硬接因果（实际需要干旱蒸发条件）。"
            "⚠ 关键：引用的原理**本身必须是自然地理里正确的**，只是套错了地方。"
        ),
    },
}

# 允许"跨领域跑题"的档位（模拟完全没复习、连考哪一科都没抓住的极端情况）
# ⚠ 只有【很差】档，且由 _allow_cross_field() 再抽一次，只有一部分人会出现。
CROSS_FIELD_ABILITY = ("很差",)
CROSS_FIELD_RATE = 0.3


def detect_field(question) -> str:
    """判断这道题属于人文还是自然。

    返回 "人文" / "自然" / ""（判不出来=不锁定，宁可宽松）。
    判据按可信度排序：主题 > 设问+材料。
    """
    topic = str(getattr(question, "topic", "") or "")
    stem = str(getattr(question, "stem", "") or "")
    material = str(getattr(question, "material", "") or "")
    body = f"{stem}\n{material}"

    def score(text: str, keys) -> int:
        return sum(1 for k in keys if k in text)

    # 主题字段是教师亲手标的，最可信——先看它
    t_h = score(topic, _HUMAN_KEYS)
    t_p = score(topic, _PHYSICAL_KEYS)
    if t_h != t_p:
        return "人文" if t_h > t_p else "自然"

    # 主题判不出来（或势均力敌）→ 退到题面
    b_h = score(body, _HUMAN_KEYS)
    b_p = score(body, _PHYSICAL_KEYS)
    if b_h != b_p:
        return "人文" if b_h > b_p else "自然"
    return ""      # 两个都不占优 → 不锁定


def _wrong_samples_for(kind: str, field: str) -> str:
    """取某一类错误在指定领域下的改写示例。

    ⚠ 领域判不出来（field=""）时，**不能退回旧的全局示例**——
      旧示例全是自然地理的，人文题会因此串领域（就是第 66 批那个 bug）。
      这种情况给一句不带任何领域词汇的通用说明，让 AI 自己贴题写。
    """
    if field in WRONG_SAMPLES_BY_FIELD:
        return WRONG_SAMPLES_BY_FIELD[field].get(kind, "")
    return (
        "把那条本来该讲的内容，换成**同类但不相干**的方面——"
        "引用的知识本身要正确，只是方向不对、因果说反、或套错了地方。"
        "⚠ 必须留在题目本身讨论的领域内，不要扯到题目没涉及的其它地理话题上去。"
    )


def _field_rule_text(field: str, cross_ok: bool) -> str:
    """拼一段"错误必须留在本领域内"的硬约束（放在错误指令的示例后面）。

    ⚠ 刻意【不列举】另一科的具体名词。原来写"严禁出现气候、地形、地质"
      反而把自然名词摆到 AI 眼前，它容易顺手用上（实测：否定式列举同样会
      污染输出）。这里只说"另一科"，用概念约束而不是词汇清单。
    """
    if not field:
        return (
            "⚠ 这些错误必须**留在题目本身讨论的地理领域内**，"
            "不要扯到题目没涉及的其他话题。"
        )
    other = "自然" if field == "人文" else "人文"
    text = (
        f"⚠⚠ 本题是【{field}地理】题，错误必须**留在{field}领域内部**"
        f"（在同类因素之间偏移）。\n"
        f"    **严禁**把话题扯到{other}地理上去——差生也是围绕题目问的方向"
        f"瞎答，不会整道题串到另一科。"
    )
    if cross_ok:
        text += (
            f"\n    唯一的例外：你是极少数完全没复习的学生，"
            f"可以把**最多一条**写成跨领域的（用{other}地理的词），"
            f"但不能每条都跨。"
        )
    return text


def wrong_quota(ability: str) -> Tuple[int, int]:
    """这一档允许犯几条错——(下限, 上限)。"""
    return WRONG_QUOTA.get(ability, (1, 2))


def pick_wrong_kinds(ability: str, rng: Optional[random.Random] = None) -> List[str]:
    """按配额抽这一份答卷要犯哪几类错误。

    返回的每一项都会原样写进给AI 的角色提示词，
    所以这里只管"抽哪几类"，具体怎么写由 AI 自己组织。
    """
    r = rng or random
    lo, hi = wrong_quota(ability)
    if hi <= 0:
        return []
    k = r.randint(max(0, lo), hi)
    if k <= 0:
        return []
    # 三类等概率抽取，允许重复（同一类错两次也符合真实情况）
    return [r.choice(WRONG_KINDS) for _ in range(k)]


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
#
# ★【2026-10-05 大改】水平差异的载体从"篇幅"换成了"错误类型"。
#   起因：第 53 份实测得分率 100%。旧版中等档靠"只罗列现象、很少推因果"
#   压分，而阅卷端放宽因果连接之后这条路作废了。
#   现在五档的差异靠两样东西：
#     ① 【故意写错的配额】——见WRONG_QUOTA，由 role 里最后那段注入
#     ② 【写对的那些条只写现象不写分析】——阅卷端据此给 1 分档
#   ⚠ 重要：三档的"写得浅"描述里**不能再出现"少写几条"**，
#     用户要求差生也写满 4~5 条。
ROLE_PROMPTS: Dict[str, str] = {
    "优秀": "你是一名地理基础扎实、平时考试能拿高分的高中生。"
            "认真审题，把你能想到的地理过程、因果关系写清楚，尽量答完整。",
    "良好": "你是一名地理成绩中上、偶尔会丢细节的高中生。"
            "按自己的理解作答，主要方向能写对，但不必追求面面俱到。"
            "写对的那些条，道理能说通，但不一定把机制讲到底。",
    # ★ 中等：写满、写对核心事实，但只写现象不写分析 → 阅卷端给 1 分档。
    #   注意措辞：不再说"想到多少写多少"（那会写成残句、口语），
    #   而是要求【每条都通顺完整、书面语】，只是不往深里推。
    "中等": "你是一名地理成绩中等的学生。你能答出大部分题目的核心事实，"
            "术语也基本用对，但**不擅长往下推一步说理**。"
            "你的每一条都是通顺完整的书面句子，写清了现象或条件，"
            "可后面「所以会怎么样」「为什么是这样」那一层就不写了，"
            "或者只写一句很笼统的话带过。想到几条就写几条，写满为止。",
    "薄弱": "你是一名地理基础较弱的学生。你能想起一些常见的地理事实，"
            "写成句子是通顺的，但**因果经常说不到位**，"
            "有时候会把因果关系说反，或者把别处才成立的道理套到这儿来。"
            "你不会察觉自己的问题，只是按自己的理解写。"
            "写满该写的条数，但每条的水平参差不齐。",
    "很差": "你是一名地理基础很差的学生。这次考试你几乎没复习，"
            "脑子里只有一些零散、模糊的地理印象。"
            "你能把句子写通顺、用上地理书面语，但**内容经常是错的**——"
            "要么讨论的根本不是题目问的那个方向，要么把因果说反，"
            "要么把不相干的地理原理硬套过来。"
            "你自己也说不清对错，写满该写的条数就是了。",
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


def student_role(
    ability: str, style: str = "", wrong_kinds: Optional[List[str]] = None,
    n_lines: Optional[int] = None, role_hint: str = "",
    question=None,
) -> str:
    """把档位 + 画像 + 错误配额翻译成给 AI 的角色提示词。

    这是生成答卷时唯一能给AI 的"控制信号"（评分细则不发给它）。

    ⚠ 2026-10-05 新增 wrong_kinds（这一份要故意犯哪几类错）
      与 n_lines（这一份大概写几条）。没传就现抽，测试里可显式传固定值。
    ★ 2026-10-05 第 66 批新增 question：用来判断题目考的是人文还是自然，
      据此锁定"错误内容必须留在同科领域内"，防止人文题里冒出气候地形。
      不传 question 时不锁定（老行为，测试与旧调用零影响）。
    ⚠ 三段拼接的顺序不能变：角色 → 画像习惯 → 故意写错。
      最后一段要单独成段，AI 才知道这是本次的额外要求，
      而不是它自己性格的一部分。

    ★★ 2026-10-05 第二轮：role_hint 来自考生档案库，与错误指令【叠加】★★
      用户定的接入方式：
        · 档案 role_hint 负责"这个人是谁"（背景、习惯、错在哪）
        · planner 的点名指令负责"第几条写成什么错"
      两者缺一不可——实测只喂 role_hint 时 AI 五条全写对，错误一条都进不去；
      只喂点名指令时人物没有个性，一份份长得一个样。
      ⚠ role_hint 为空 → 完全退回旧行为（用 ROLE_PROMPTS + STYLE_TRAITS），
        老流程和测试零影响。

    ★★ 为什么要指定【具体条号】★★
      实测（第 53 份）：只在提示词里说"你这次有 1 处会答错"，
      AI 写出来的 6 条**全是对的**——它天生倾向交一份好卷子，
      抽象的"犯个错"对它没有约束力。
      改成"第 3 条必须写成『张冠李戴』"之后才有可执行的指令。
      这跟"抽点限制作答范围"是同一个思路：
      **不给死条号，它就每条都写成满分答案。**
    """
    # 人物设定：优先用档案给的 role_hint，其次是"档位提示词 + 答题习惯"
    if role_hint:
        tail = role_hint
    else:
        tail = ROLE_PROMPTS.get(ability, ROLE_PROMPTS["中等"])
        trait = STYLE_TRAITS.get(style, "")
        if trait:
            tail += f" 你的答题习惯（{style}）：{trait}"

    kinds = wrong_kinds if wrong_kinds is not None else pick_wrong_kinds(ability)
    # ★★顺序很关键：错误指令放【最前面】★★
    #   实测（第 53 份）：错误指令写在角色描述之后时，AI 基本无视它——
    #   角色的力量远大于"要写错"这句附带要求。提到最前面（并加粗强调）
    #   之后命中率明显上升。
    #   人物设定反而可以放后面，它只是背景，不是本次的重点。
    if kinds:
        # ★ 学科领域锁定：错误示例必须跟题目同科（2026-10-05 第 66 批修复）
        field = detect_field(question) if question is not None else ""
        cross_ok = bool(
            question is not None
            and ability in CROSS_FIELD_ABILITY
            and random.random() < CROSS_FIELD_RATE
        )
        return (
            f"{_wrong_instruction(kinds, n_lines, field, cross_ok)}\n"
            f"【人物设定】{tail}"
        )

    return tail


def _wrong_instruction(
    kinds: List[str], n_lines: Optional[int] = None,
    field: str = "", cross_ok: bool = False,
) -> str:
    """把"要犯哪几类错"翻译成带条号的硬指令。

    条号怎么选：
      · 第 1 条通常写得最像样、最容易命中采分点，把它写错代价太大，
        所以错误**从第 2 条起**往后排。
      · 均匀铺开而不是挤在一起——真实卷子的错误本来就散在各处。
      · 如果要写的条数还没定（n_lines 为空），按中等档的 5 条估。

    ★ 2026-10-05 新增 field / cross_ok：**学科领域锁定**
      field = "人文" / "自然" / ""
      · 有值时：错误示例换成该领域的，并在最前面加一条硬约束
        "严禁出现另一科的名词"
      · cross_ok=True 时额外放行"最多一条跨领域"——只给很差档的一小部分人
    """
    n = max(2, int(n_lines or 5))
    # 错误条数不能占满——至少留一半条目是对的。
    # ⚠ 实测：很差档写 4 条、错 2 条时，卷面上一半都是错句，
    #   反而不像真实考生（真实卷子错的都是零星几条）。
    #   上限压到 n 的三分之一，保证"对的比错的多"。
    cap_by_n = max(1, n // 3)
    k = min(len(kinds), n - 1, cap_by_n)
    pool = list(range(2, n + 1))          # 从第 2 条开始
    # 均匀铺开：错误散在各处，不挤在末尾，也不集中在开头。
    # （实测：末尾集中会让 AI 觉得"最后一条写错就行"，
    #   于是把它写对、把中间某条留成错句，反而不可控。）
    if k == 1:
        slots = [pool[len(pool) // 2]] if len(pool) > 2 else [pool[0]]
    else:
        step = (len(pool) - 1) / (k - 1)
        slots = [pool[int(round(i * step))] for i in range(k)]
    uniq: List[int] = []
    for s in slots:
        if s not in uniq:
            uniq.append(s)
    # ★ 示例必须按【题目领域】取——旧的全局示例全是自然地理的，
    #   人文题照着套就会串领域（第 66 批的 bug 就是这么来的）。
    pairs = "；".join(
        f"第 {uniq[i]} 条写成「{kinds[i]}」——{_wrong_samples_for(kinds[i], field)}"
        for i in range(len(uniq))
    )
    # ★ 为什么要"先想对的、再点名改错"（实测踩过）：
    #   只说"这条必须写错"时，AI 十次有八次会把这条也写成对的——
    #   它太擅长交好卷子了。改成两步（先默认真解、再改错），
    #   命中率明显提高：AI 必须先想出正确版本，才有东西可改。
    return (
        f"★★★ 这一份答卷的写法分两步，请严格照做★★★\n"
        f"【第一步】先在心里把全部 {n} 条都想成**你水平该有的正确样子**，"
        f"不要一开始就想着写错。\n"
        f"【第二步】然后**点名改错**这 {len(uniq)} 条：{pairs}。"
        f"其余 {n - len(uniq)} 条保持第一步想出来的正确样子。\n"
        f"{_field_rule_text(field, cross_ok)}\n"
        f"★ 这{len(uniq)} 条【不许写对】。AI 最常见的毛病是「忘记改」——"
        f"你会不自觉把它们写对，务必刻意把它们改坏。\n"
        f"⚠ 错误必须【听着有道理】：引用的地理知识本身要像真的，"
        f"只是方向答错、因果说反、或把别的原理套过来；"
        f"严禁写成「{_absurd_sample(field)}」这种一眼就看出不对的话。\n"
        f"⚠ 你自己察觉不到这些错误，就当它们是对的知识写下去。"
    )


def _absurd_sample(field: str) -> str:
    """给"严禁写成一眼看出的蠢话"配一个例子。

    ⚠ 例子必须跟题目同科——人文题举"海拔越高气温越高"会顺手把自然名词
      塞进 AI 视野（第 66 批那个 bug 的同类问题）。
    """
    if field == "人文":
        return "该地人口越多，所以人口越少"
    return "海拔越高，气温越高"


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
    profiles: Optional[List[dict]] = None,
) -> List[StudentPlan]:
    """为一整批学生生成"设计档位"和"错误画像"。

    ★ 2026-10-05 新增 profiles（考生档案库抽出来的那几份，见 student_profiles.json）
      传了 profiles 时：
        · ability / style 从档案取，不再按 weights 现抽
        · wrong_kinds 用档案的 error_tendencies（**只保留可注入的三类**，
          漏点/堆材料是卷面形态，不算"某一条写错"），不再由 pick_wrong_kinds 现抽
        · role_hint 原样存进 plan，生成时当人物设定用
      不传 profiles 时，行为跟以前一模一样（老流程、测试零影响）。

    ⚠ 为什么错误类型必须来自档案而不是现抽：
      档案的 role_hint 里写着"你会犯因果颠倒"，如果 wrong_kinds 是现抽的，
      点名到"第 3 条"的错误可能跟档案说的不是同一类——
      于是提示词自相矛盾，AI 只能挑一个听，实测落地率很低。
    """
    scores = {p.seq: p.score for p in question.points}
    n = len(question.points)
    score_list = [p.score for p in question.points]
    plans: List[StudentPlan] = []
    pool = list(profiles) if profiles else []

    for i in range(count):
        prof = pool[i] if i < len(pool) else None

        if prof:
            ability = prof.get("level") or pick_ability(weights)
            style = prof.get("style") or ""
            role_hint = prof.get("role_hint") or ""
            # 只取可注入的三类；漏点/堆材料走"卷面形态"，不进错误条数配额
            kinds = [k for k in (prof.get("error_tendencies") or [])
                     if k in WRONG_KINDS]
        else:
            ability = pick_ability(weights)
            style = pick_style(ability, style_weights)
            role_hint = ""
            kinds = None      # None = 交给 student_role 现抽（旧行为）

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
                # ★ 这一份故意要犯哪几类错（2026-10-05 新增）。
                #   抽一次就存下来，生成和闸门共用同一份名单——
                #   闸门据此放行"故意的答非所问"，否则会把它当硬伤打回。
                #   有档案时用档案的 error_tendencies，没有时现抽。
                wrong_kinds=kinds if kinds is not None else pick_wrong_kinds(ability),
                # ★ 档案的人设，跟 wrong_kinds 叠加使用（不是替代）
                role_hint=role_hint,
            )
        )
    return plans
