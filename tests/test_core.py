"""纯逻辑自测，不需要联网、不需要密钥。

运行方式：
    .venv\\Scripts\\python.exe tests\\test_core.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.aggregator import aggregate, vote  # noqa: E402
from core.models import (  # noqa: E402
    GradingRun, PointVerdict, Question, RubricPoint, Student, StudentPlan,
)
from core.planner import make_plans, split_points  # noqa: E402
from core.report import compare, verdict_text  # noqa: E402
from services.prompts import fill, load_prompt  # noqa: E402

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  通过  {name}")
    else:
        print(f"  失败  {name} {extra}")
        FAILED.append(name)


def test_planner():
    print("\n[分档逻辑]")
    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=8,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 5)],
    )
    # 与 config.yaml 保持一致：完全命中 10% / 半对 70% / 未命中 20%
    weights = {"优秀": 0.10, "良好": 0.20, "中等": 0.30, "薄弱": 0.20, "很差": 0.20}
    style_weights = {
        "浮于表面型": 0.35, "似是而非型": 0.25, "只报结论型": 0.15,
        "术语笼统型": 0.10, "半途而止型": 0.15,
    }
    plans = make_plans(q, 400, weights, blank_rate=0.15, style_weights=style_weights)
    check("生成 400 份设计档位", len(plans) == 400)

    for p in plans:
        all_idx = sorted(p.hit + p.partial + p.off + p.blank)
        if all_idx != [1, 2, 3, 4]:
            check("每个采分点都被分配且只分配一次", False, str(p))
            return
    check("每个采分点都被分配且只分配一次", True)

    high = [p for p in plans if p.ability == "优秀"]
    low = [p for p in plans if p.ability == "很差"]
    if high and low:
        avg_high = sum(p.design_score for p in high) / len(high)
        avg_low = sum(p.design_score for p in low) / len(low)
        check("优秀档设计分高于很差档", avg_high > avg_low,
              f"{avg_high:.2f} vs {avg_low:.2f}")
    check("存在整点漏答的答卷", any(p.blank for p in plans))

    hit, part, off, blank = split_points(4, "优秀", 0.15)
    check("优秀档不含未命中的点", off == [], str(off))

    # 设计主线的硬指标：中等档应该是"对一条、半对一条、废一条"的混合，
    # 而不是"每条都半对"——用户明确点过这一点。
    mid = [p for p in plans if p.ability == "中等"]
    m_part = sum(len(p.partial) for p in mid) / max(1, len(mid))
    m_hit = sum(len(p.hit) for p in mid) / max(1, len(mid))
    m_off = sum(len(p.off) for p in mid) / max(1, len(mid))
    check("中等档是'完整 + 部分 + 未命中'的混合（不是每条都半对）",
          m_part >= 1.5 and m_hit >= 0.6 and m_off >= 0.6,
          f"完整 {m_hit:.2f} / 部分 {m_part:.2f} / 未命中 {m_off:.2f}")

    weak = [p for p in plans if p.ability == "薄弱"]
    w_hit = sum(len(p.hit) for p in weak) / max(1, len(weak))
    w_part = sum(len(p.partial) for p in weak) / max(1, len(weak))
    check("薄弱档几乎没有完整命中的点，但仍有部分命中",
          w_hit < 1.0 and w_part > 0.8, f"完整 {w_hit:.2f} / 部分 {w_part:.2f}")

    # 全班范围内，"部分命中"必须是点数最多的类型（半吊子是主角）
    tot_part = sum(len(p.partial) for p in plans)
    tot_hit = sum(len(p.hit) for p in plans)
    tot_off = sum(len(p.off) for p in plans)
    check("全班点数以'部分命中'最多", tot_part > tot_off and tot_part > tot_hit,
          f"部分 {tot_part} / 未命中 {tot_off} / 完整 {tot_hit}")


def test_draw_range():
    """第一步"抽点"：严禁把池子里的点全给学生。"""
    print("\n[抽点 · 答题范围]")
    from core.planner import draw_range, split_points

    # 用户举的例子：6 分的题、5 个采分点 → 随机抽 3~4 个点，绝不给 5 个
    lo, hi = draw_range(5, 6.0, 2.0)
    check("6 分 / 5 点的题，抽点范围是 3~4", lo == 3 and hi == 4, f"{lo}~{hi}")

    lo, hi = draw_range(4, 8.0, 2.0)      # 没有候补点：只能全给
    check("没有候补点时（8 分 / 4 点）范围是 4~4", lo == 4 and hi == 4, f"{lo}~{hi}")

    lo, hi = draw_range(6, 10.0, 2.0)     # 10 分 / 6 点
    check("10 分 / 6 点的题不给满 6 个", hi <= 5, f"{lo}~{hi}")

    # 端到端：跑 300 份，任何一份抽到的点数都不能等于池子大小
    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=6,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 6)],
    )
    plans = make_plans(q, 300, {"中等": 1.0}, blank_rate=0.0)
    got = {len(p.hit) + len(p.partial) + len(p.off) for p in plans}
    check("没有任何一份答卷把 5 个点全写了", 5 not in got, str(sorted(got)))
    check("抽点数量落在 3~4 之间", got <= {3, 4}, str(sorted(got)))
    check("每份都留了没答到的点", all(p.blank for p in plans))

    hit, partial, off, blank = split_points(5, "中等", 0.0, [2.0] * 5, 6.0)
    score = 2 * len(hit) + 1 * len(partial)
    check("中等生的设计分约 3 分（6 分的一半）",
          2.5 <= score <= 3.5, f"{score} 分：完整{len(hit)} 部分{len(partial)} 未命中{len(off)}")
    check("三部分都占到了（完整 + 部分 + 未命中）",
          len(hit) >= 1 and len(partial) >= 1 and len(off) >= 1,
          f"完整{len(hit)} 部分{len(partial)} 未命中{len(off)}")


def test_distribution():
    """守住"不能全员满分"这一条：半对要占七成，满分卷要极少，
    并且全班平均得分率要被死死压在四分之一上下。"""
    print("\n[水平分布]")
    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=8,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 5)],
    )
    weights = {"优秀": 0.10, "良好": 0.20, "中等": 0.30, "薄弱": 0.20, "很差": 0.20}
    plans = make_plans(q, 600, weights, blank_rate=0.15)

    half = [p for p in plans if p.ability in ("良好", "中等", "薄弱")]
    full = [p for p in plans if p.ability == "优秀"]
    none_ = [p for p in plans if p.ability == "很差"]
    r_half = len(half) / len(plans)
    r_full = len(full) / len(plans)
    r_none = len(none_) / len(plans)

    check("部分命中（半对）约占 70%", 0.62 <= r_half <= 0.78, f"{r_half:.1%}")
    check("完全命中约占 10%", 0.05 <= r_full <= 0.16, f"{r_full:.1%}")
    check("未命中约占 20%", 0.14 <= r_none <= 0.26, f"{r_none:.1%}")

    rate = sum(p.design_score for p in plans) / len(plans) / q.max_score
    check("全班平均得分率被压在 30%~60%（不能全是满分）",
          0.30 <= rate <= 0.60, f"{rate:.1%}")

    perfect = [p for p in plans if p.design_score >= q.max_score]
    check("满分卷比例低于 15%", len(perfect) / len(plans) < 0.15,
          f"{len(perfect) / len(plans):.1%}")


def test_styles():
    print("\n[错误画像]")
    valid = {"浮于表面型", "似是而非型", "只报结论型", "术语笼统型",
             "半途而止型", "扎实型"}
    weights = {"优秀": 0.10, "良好": 0.20, "中等": 0.30, "薄弱": 0.20, "很差": 0.20}
    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=8,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 5)],
    )
    plans = make_plans(q, 300, weights, blank_rate=0.15)
    check("每份答卷都有错误画像", all(p.style for p in plans))
    check("画像取值合法", all(p.style in valid for p in plans),
          str({p.style for p in plans}))
    check("优秀档固定为扎实型",
          all(p.style == "扎实型" for p in plans if p.ability == "优秀"))
    check("很差档只用浮于表面/半途而止",
          all(p.style in ("浮于表面型", "半途而止型")
              for p in plans if p.ability == "很差"))
    check("没有任何一份答卷是'答非所问型'（这类已取消）",
          all(p.style != "答非所问型" for p in plans))
    half_styles = {p.style for p in plans if p.ability in ("良好", "中等", "薄弱")}
    check("五类半吊子画像都被用到", len(half_styles) == 5, str(half_styles))

    # 逐点缺陷要求：每个采分点都要有，且与所属类别一致
    from core.planner import (
        HINT_BLANK, HINT_HIT, OFF_HINTS, PARTIAL_HINTS,
    )
    bad = 0
    for p in plans:
        if set(p.defects.keys()) != {1, 2, 3, 4}:
            bad += 1
            continue
        for s in p.hit:
            if p.defects[s] != HINT_HIT:
                bad += 1
        for s in p.partial:
            if p.defects[s] not in PARTIAL_HINTS:
                bad += 1
        for s in p.off:
            if p.defects[s] not in OFF_HINTS:
                bad += 1
        for s in p.blank:
            if p.defects[s] != HINT_BLANK:
                bad += 1
    check("每个采分点都有与之匹配的缺陷要求", bad == 0, f"{bad} 处不匹配")

    # 沾边点的要求必须是"只写表面 / 似是而非"类写法（本次的两条主线）
    part_texts = [p.defects[s] for p in plans for s in p.partial]
    check("部分命中的要求全是'浮于表面/似是而非/只报结论/术语笼统/半途而止'类写法",
          all(("浮于表面" in t) or ("似是而非" in t) or ("只报结论" in t)
              or ("术语笼统" in t) or ("半途而止" in t)
              for t in part_texts),
          str(sorted(set(part_texts))[:3]))
    check("似是而非型不允许低级错误（红线写进了下发要求）",
          all(("低级" in t and "张冠李戴" in t)
              for t in part_texts if "似是而非" in t))

    off_texts = [p.defects[s] for p in plans for s in p.off]
    check("未命中的点全用'未命中'类写法（不再出现'答非所问'）",
          all("未命中" in t for t in off_texts),
          str(sorted(set(off_texts))[:3]))
    check("缺陷要求里彻底没有'答非所问'字样",
          not any("答非所问" in t for t in part_texts + off_texts))
    hit_texts = [p.defects[s] for p in plans for s in p.hit]
    check("完整命中的点要求'三环齐全 + 不照抄细则'",
          all("完整命中" in t and "严禁照抄" in t for t in hit_texts),
          str(sorted(set(hit_texts))[:2]))


def test_aggregator():
    print("\n[投票汇总]")

    check("众数投票", vote([2, 2, 1]) == (2, 2 / 3))
    check("完全相同", vote([0, 0, 0]) == (0, 1.0))
    level, agree = vote([0, 1, 2])
    check("三方平局取中间值", level == 1, str(level))

    q_points = [RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 4)]
    runs = [
        GradingRun(0, [
            PointVerdict(1, 2, "完全正确", "原句A"),
            PointVerdict(2, 1, "只说了一半", "原句B"),
            PointVerdict(3, 0, "方向错了", ""),
        ], comment="整体还行"),
        GradingRun(1, [
            PointVerdict(1, 2, "完全正确", "原句A"),
            PointVerdict(2, 2, "写全了", "原句B"),
            PointVerdict(3, 0, "方向错了", ""),
        ], comment="整体还行"),
        GradingRun(2, [
            PointVerdict(1, 2, "完全正确", "原句A"),
            PointVerdict(2, 1, "只说了一半", "原句B"),
            PointVerdict(3, 0, "方向错了", ""),
        ], comment="还行"),
    ]
    res = aggregate(1, q_points, runs)
    check("总分按各点加权求和", res.total == 3.0, str(res.total))
    check("第2点取众数 1", res.points[1].level == 1)
    check("置信度 = 一致率均值", abs(res.confidence - (1 + 2 / 3 + 1) / 3) < 0.01,
          str(res.confidence))
    check("多次总分波动被记录", res.score_range == 1.0, str(res.score_range))
    check("证据取自与最终档位一致的调用", res.points[1].evidence == "原句B")

    empty = aggregate(2, q_points, [GradingRun(0, [])])
    check("AI 返回空时判 0 分且标记低置信", empty.total == 0 and empty.low_confidence)


def test_report():
    print("\n[对比统计]")
    students = [Student(seq=i, ability="中等", answer="x") for i in range(1, 6)]
    teacher = {1: 4, 2: 6, 3: 2, 4: 8, 5: 0}
    ai = {}
    for i in range(1, 6):
        ai[i] = aggregate(
            i,
            [RubricPoint(seq=j, text=f"点{j}", score=2) for j in range(1, 5)],
            [GradingRun(0, [
                PointVerdict(j, min(2, max(0, (teacher[i] // 2) - (j - 1))), "", "")
                for j in range(1, 5)
            ])],
        )
    stats = compare(students, teacher, ai)
    check("统计份数正确", stats["n"] == 5)
    check("平均分计算正确", stats["avg_teacher"] == 4.0, str(stats["avg_teacher"]))
    check("完全一致的能识别", stats["exact_rate"] >= 0, str(stats["exact_rate"]))
    verdict, color = verdict_text(stats)
    check("能给出文字结论", bool(verdict))


def test_quality_gate():
    """守住'照抄评分标准'这条底线。"""
    print("\n[生成质量闸门]")
    from core.quality import (
        clean_answer, find_violations, force_fix, longest_common, similarity,
    )

    check("完全相同的文本重合度为 1",
          abs(similarity("夏季受副热带高气压带控制", "夏季受副热带高气压带控制") - 1.0) < 0.01)
    check("完全无关的文本重合度为 0",
          similarity("夏季受副热带高气压带控制", "该地位于板块交界处多火山地震") == 0.0)

    # 关键场景：只抄了采分点的后半段，整体重合度被稀释，但连续片段藏不住
    point = "上游流经高山峡谷，落差大，水流湍急"
    copied = "该河段的落差大，水流湍急，适合开发水电"
    ratio = similarity(point, copied)
    common = longest_common(point, copied)
    check("只抄后半段时整体重合度确实偏低", ratio < 0.6, f"{ratio:.0%}")
    check("但最长连续雷同片段能抓到照抄（阈值 6 字）", common >= 6, f"{common} 字")

    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=6,
        points=[
            RubricPoint(seq=1, text="上游落差大水流湍急", score=2),
            RubricPoint(seq=2, text="流域降水丰富径流量大", score=2),
            RubricPoint(seq=3, text="无结冰期可全年通航", score=2),
        ],
    )
    plan = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                       hit=[1], partial=[2], off=[], blank=[3])

    # 合格的答案：第2点只写了条件（因果链没写）、第3点完全不写
    good = "1. 上游落差大水流湍急，水能丰富。\n2. 这里降水比较多。"
    check("合格答卷不会被判违规", find_violations(q, plan, good) == [],
          str(find_violations(q, plan, good)))

    # 该"只写表面"的点把因果链写全了
    bad_inc = ("1. 上游落差大水流湍急，水能丰富。\n"
               "2. 流域降水丰富径流量大，水量充足。")
    v = find_violations(q, plan, bad_inc)
    check("该写浅的点写到位了会被检出", any("第2点" in x for x in v), str(v))

    # 更隐蔽的情况：只抄了后半段（整体重合度不高，但连续片段够长）
    bad_half = ("1. 上游落差大水流湍急。\n"
                "2. 降水丰富径流量大，河流水量充足。")
    v = find_violations(q, plan, bad_half)
    check("只抄后半段也能被检出", any("第2点" in x for x in v), str(v))

    # 该"未作答"的点被写出来了
    bad_blank = ("1. 上游落差大水流湍急。\n"
                 "2. 降水较多。\n"
                 "3. 无结冰期可全年通航，航运价值高。")
    v = find_violations(q, plan, bad_blank)
    check("未作答的点被写出会被检出", any("第3点" in x for x in v), str(v))

    # 答对的点写得再像也不该被判违规
    plan_hit = StudentPlan(seq=2, ability="优秀", style="扎实型",
                           hit=[1, 2, 3], partial=[], off=[], blank=[])
    full = "1. 上游落差大水流湍急。\n2. 流域降水丰富径流量大。\n3. 无结冰期可全年通航。"
    check("答对的点写得再标准也不判违规",
          find_violations(q, plan_hit, full) == [],
          str(find_violations(q, plan_hit, full)))

    # ---- 未命中（0 分档：写了但拿不到分）----
    plan_off = StudentPlan(seq=3, ability="中等", style="浮于表面型",
                           hit=[1], partial=[], off=[2], blank=[3])
    proper_off = ("1. 上游落差大水流湍急，水能丰富。\n"
                  "2. 当地的自然条件对沿岸有一定帮助。")
    check("只丢一句空话、没答到点上的那条不算违规",
          find_violations(q, plan_off, proper_off) == [],
          str(find_violations(q, plan_off, proper_off)))

    answered_right = ("1. 上游落差大水流湍急。\n"
                      "2. 流域降水丰富径流量大，水量充足。")
    v = find_violations(q, plan_off, answered_right)
    check("该未命中的点却答到位了，会被检出",
          any("第2点" in x for x in v), str(v))

    # ---- 方向红线：问自然条件就不许出现经济、人口 ----
    q_dir = Question(
        subject="地理", topic="农业", max_score=8,
        stem="【设问】分析该地种植水稻的有利自然条件。（8分）",
        points=[RubricPoint(seq=1, text="昼夜温差大：白天光合作用强", score=2)],
    )
    forbidden = {"natural": ["人口", "经济"], "favorable": ["崎岖", "不足"]}
    plan_dir = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                           hit=[], partial=[1], off=[], blank=[])
    v = find_violations(q_dir, plan_dir,
                        "1. 该地人口稀少，经济落后。", forbidden=forbidden)
    check("问自然条件却写人口/经济 → 抓得住",
          any("方向红线" in x for x in v), str(v))
    check("咬住自然条件写的答案不判违规",
          find_violations(q_dir, plan_dir, "1. 当地昼夜温差比较大。",
                          forbidden=forbidden) == [],
          str(find_violations(q_dir, plan_dir, "1. 当地昼夜温差比较大。",
                              forbidden=forbidden)))
    q_two = Question(
        subject="地理", topic="评价", max_score=8,
        stem="【设问】评价该工程带来的有利与不利影响。（8分）",
        points=[RubricPoint(seq=1, text="淹没耕地：移民安置压力大", score=2)],
    )
    check("两问的题不启用负面词表（不误伤正确作答）",
          find_violations(q_two, StudentPlan(seq=1, ability="优秀"),
                          "1. 工程会淹没部分耕地。", forbidden=forbidden) == [])

    # ---- 同义改写（换掉动词、名词还在）：文本比对抓不到，只能靠提示词约束 ----
    syn_point = "排水不畅：河道弯曲，湖泊众多，调蓄能力有限"
    syn_text = "该区域湖泊星罗棋布，河道走向曲折，水流在低洼地带容易滞留，排水效率不高。"
    check("同义改写确实抓不到（已知局限，靠提示词兜）",
          similarity(syn_point, syn_text) < 0.45 and longest_common(syn_point, syn_text) < 6,
          f"重合度 {similarity(syn_point, syn_text):.2f}／连续 {longest_common(syn_point, syn_text)} 字")

    q8 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=4,
        points=[
            RubricPoint(seq=1, text=syn_point, score=2),
            RubricPoint(seq=2, text="流量大：流域面积广，降水丰富", score=2),
        ],
    )
    plan_syn = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                           hit=[2], partial=[], off=[1], blank=[])
    hit_line = "2. 该河段流域面积广，降水丰富，水量大。"
    honest_off = "1. 该地区湖泊比较多。\n" + hit_line
    check("没答到点上的答案不会被误判（不搞'用词重复'那种字面判据）",
          find_violations(q8, plan_syn, honest_off) == [],
          str(find_violations(q8, plan_syn, honest_off)))

    # 本地兜底改写（备用开关，默认关闭，见 config.yaml 的 force_fix）
    fixed = force_fix(q, plan, bad_blank)
    check("本地兜底会删掉'未作答'的那一句",
          "无结冰期可全年通航" not in fixed, fixed)
    check("本地兜底不会把答案清空", len(fixed.strip()) > 5, fixed)
    check("兜底后不再出现双句号", "。。" not in fixed, fixed)

    fixed_off = force_fix(q, plan_off, answered_right)
    check("兜底会删掉'该偏却答对'的那一句",
          "流域降水丰富" not in fixed_off, fixed_off)
    check("删掉偏题点后仍保留答对的内容",
          "上游落差大" in fixed_off, fixed_off)

    # 采分点长短不同，"照抄"的门槛必须跟着浮动：
    # 22 字的长采分点里，学生只引用了 6 个字——那还是"沾边没答到"的半对答案
    q2 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=2,
        points=[RubricPoint(
            seq=1, text="长江中下游地势低平排水缓慢水位上涨持续时间长", score=2)],
    )
    plan2 = StudentPlan(seq=1, ability="薄弱", style="浮于表面型",
                        hit=[], partial=[1], off=[], blank=[])
    partial = "1. 长江中下游地区地势低平"
    check("长采分点只引用半句不算照抄", find_violations(q2, plan2, partial) == [],
          str(find_violations(q2, plan2, partial)))
    check("长采分点半对答案不会被本地改动",
          force_fix(q2, plan2, partial) == partial)

    # 但短采分点被整句照抄就必须处理，且不能留下"区地势低平"这种断头句
    q3 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=2,
        points=[RubricPoint(seq=1, text="无结冰期可全年通航", score=2)],
    )
    plan3 = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                        hit=[], partial=[1], off=[], blank=[])
    copied = "1. 无结冰期可全年通航。"
    check("短采分点被整句照抄会被检出",
          find_violations(q3, plan3, copied) != [])
    broken = force_fix(q3, plan3, copied)
    check("兜底不会留下断头句", "无结冰期" not in broken, broken)
    check("兜底后与标准表述不再连续雷同",
          longest_common(q3.points[0].text, broken) < 6, broken)
    check("兜底改写后仍是通顺的一句话",
          broken.endswith("。") and len(broken) > 8, broken)

    # 删掉整条后要重新编号，并清掉"2."这种空条
    plan4 = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                        hit=[1], partial=[], off=[], blank=[3])
    messy = "1. 上游落差大水流湍急。\n2. \n3. 无结冰期可全年通航。"
    cleaned = force_fix(q, plan4, messy)
    check("兜底后清掉空条并重新编号",
          cleaned.strip() == "1. 上游落差大水流湍急。", repr(cleaned))

    check("清理分值标记", "（2分）" not in clean_answer("该地落差大（2分），水能丰富"))
    check("清理多余空行", "\n\n" not in clean_answer("第一行。\n\n\n第二行。"))
    check("清掉空条号并重新编号",
          clean_answer("1. 甲。\n2. \n4. 乙。") == "1. 甲。\n2. 乙。",
          repr(clean_answer("1. 甲。\n2. \n4. 乙。")))
    check("重复标点被压成一个",
          clean_answer("该地落差大。。水能丰富。") == "该地落差大。水能丰富。",
          repr(clean_answer("该地落差大。。水能丰富。")))
    check("断裂语病被修通顺",
          clean_answer("该地区属于，气温较高。") == "该地区气温较高。",
          repr(clean_answer("该地区属于，气温较高。")))
    check("不误伤正常的连接词",
          clean_answer("因为降水多，所以水量大。") == "因为降水多，所以水量大。",
          repr(clean_answer("因为降水多，所以水量大。")))

    # ---- 挤在一行的分点：要能自动拆开、也要数得准 ----
    # 实测：AI 偶尔把 4 条答案挤成一整行，按行数会被误判成"只写了一条"，
    # 于是白打回一轮改写（白花额度），卷面看着也不像答题卡。
    from core.quality import count_points, split_inline_points
    inline = "1. 甲地落差大。2. 乙地降水多。3. 丙地不结冰。"
    check("能按编号数出分点（挤在一行也算 3 条）", count_points(inline) == 3,
          str(count_points(inline)))
    check("挤在一行的分点会被拆成独立行",
          split_inline_points(inline).count("\n") == 2,
          repr(split_inline_points(inline)))
    check("数字里的点号不会被当成分点（约3.2万个）",
          count_points("1. 该县新增就业3.2万个。") == 1,
          str(count_points("1. 该县新增就业3.2万个。")))
    check("clean_answer 也会顺手拆行",
          clean_answer(inline).count("\n") == 2,
          repr(clean_answer(inline)))
    check("没有编号的答案按行数算",
          count_points("该地落差大。\n降水也比较多。") == 2)

    # ---- AI 自述与答案的一致性核对（防"嘴上说漏、笔下照写"）----
    from core.quality import parse_declared

    flaw = "第2点漏掉「流域降水丰富」，只写降水比较多；第3点整条不写"
    parsed = parse_declared(flaw)
    check("能解析出自述里第2点要漏掉的词",
          parsed.get(2) == ["流域降水丰富"], str(parsed))
    check("「整条不写」这类元说法不算关键词", parsed.get(3) == [], str(parsed))

    plan_inc = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                           hit=[1], partial=[2], off=[], blank=[3])
    honest = "1. 上游落差大水流湍急。\n2. 这里降水比较多。"
    check("说到做到的自述不判违规",
          find_violations(q, plan_inc, honest, flaw=flaw) == [],
          str(find_violations(q, plan_inc, honest, flaw=flaw)))

    lying = "1. 上游落差大水流湍急。\n2. 这里流域降水丰富，水量大。"
    lie_v = find_violations(q, plan_inc, lying, flaw=flaw)
    check("自述说要漏掉的词出现在答案里会被抓",
          any("流域降水丰富" in x for x in lie_v), str(lie_v))

    silent = "1. 上游落差大水流湍急。\n2. 这里降水比较多。"
    no_note = find_violations(q, plan_inc, silent, flaw="第1点答对")
    check("该偏的点没在自述里交代会被抓",
          any("没有说明" in x for x in no_note), str(no_note))

    # 未命中的点只要在自述里交代过就行，不必用「」标因果
    off_flaw = ("第1点答对；第2点只写了一句'对沿岸有一定帮助'；第3点整条不写")
    plan_off2 = StudentPlan(seq=4, ability="中等", style="浮于表面型",
                            hit=[], partial=[], off=[2], blank=[3])
    ok_off = "1. 该地降水不算多。"
    check("未命中的点不必用「」标因果",
          find_violations(q, plan_off2, ok_off, flaw=off_flaw) == [],
          str(find_violations(q, plan_off2, ok_off, flaw=off_flaw)))

    # 变相写法也要抓到：声明漏掉「降水丰富径流量」，写成"降水丰富，径流量很大"
    fuzzy_flaw = "第2点漏掉「降水丰富径流量」，只写降水较多；第3点整条不写"
    fuzzy_ans = "1. 上游落差大水流湍急。\n2. 该地降水丰富，径流量很大。"
    fuzzy_v = find_violations(q, plan_inc, fuzzy_ans, flaw=fuzzy_flaw)
    check("声明词被插字改写后仍能抓到",
          any("降水丰富径流量" in x for x in fuzzy_v), str(fuzzy_v))

    # 兜底：声明要漏掉的词，直接由本地摘掉
    cleaned2 = force_fix(q, plan_inc, lying, flaw=flaw)
    check("兜底把泄漏的声明词摘掉了", "流域降水丰富" not in cleaned2, cleaned2)

    # 强制改残时优先"删因果从句、只留结论"，而不是留半句
    q5 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=2,
        points=[RubricPoint(seq=1, text="汛期长受夏季风影响时间长雨季长", score=2)],
    )
    plan5 = StudentPlan(seq=1, ability="中等", style="只报结论型",
                        hit=[], partial=[1], off=[], blank=[])
    verbose = "1. 该地汛期长，因为受夏季风影响时间长，雨季长。"
    check("又长又准的句子会被检出",
          find_violations(q5, plan5, verbose) != [], "应判违规")
    trimmed = force_fix(q5, plan5, verbose)
    check("改残时删掉了因果从句", "因为" not in trimmed, trimmed)
    check("改残后仍保留结论", "汛期长" in trimmed, trimmed)
    check("改残后不再与标准表述雷同",
          longest_common(q5.points[0].text, trimmed) < 7, trimmed)

    # 改残不能留下"受西南，""冬季。"这类机器痕迹
    q6 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=2,
        points=[RubricPoint(seq=1, text="受东南季风影响降水集中且丰富河流补给充足",
                            score=2)],
    )
    plan6 = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                        hit=[], partial=[1], off=[], blank=[])
    raw = "1. 夏季流量大：受东南季风影响，降水集中且丰富，河流补给充足。"
    out6 = force_fix(q6, plan6, raw)
    check("改残后不留光杆介词残句",
          not re.search(r"(^|，)(受|因|由|在|从|向|对)[^，。；]{0,3}(，|。)", out6),
          out6)

    # 一份答案里最多一句"绕圈子的空话"
    q7 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=4,
        points=[
            RubricPoint(seq=1, text="受东南季风影响降水集中且丰富河流补给充足", score=2),
            RubricPoint(seq=2, text="受西北季风影响降水稀少河流补给减少", score=2),
        ],
    )
    plan7 = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                        hit=[], partial=[1, 2], off=[], blank=[])
    both = ("1. 夏季流量大：受东南季风影响，降水集中且丰富，河流补给充足。\n"
            "2. 冬季流量小：受西北季风影响，降水稀少，河流补给减少。")
    out7 = force_fix(q7, plan7, both)
    from core.quality import FILLERS
    filler_hits = sum(1 for f in FILLERS if f.rstrip("。") in out7)
    check("同一份答案最多一句空话", filler_hits <= 1, f"{filler_hits} 句：{out7}")

    # ---- YAML 的布尔键坑 ----
    # config.yaml 里没加引号的 `off:` 会被 YAML 读成布尔值 False，
    # 键名对不上就静默退回默认值——用户改了配置也白改。
    from core.quality import normalize_keys
    check("能把被 YAML 读成布尔的 off 键掰回字符串",
          normalize_keys({False: 0.6}) == {"off": 0.6},
          str(normalize_keys({False: 0.6})))
    check("键名的大小写、空格也容错",
          normalize_keys({" OFF ": 0.6, "Blank": 0.3})
          == {"off": 0.6, "blank": 0.3})
    check("正常字符串键不受影响",
          normalize_keys({"off": 0.6}) == {"off": 0.6})

    # ---- 产业题的长名词条件：部分命中只跟冒号后的因果链比 ----
    # 实测踩过（第 18 号存档）：条件是"高校培养大量电子信息专业人才"
    # 这种专有长名词，学生照实写条件就和细则连续雷同 17 字 → 误报，
    # 且 AI 改写绕不开专有名词 → "改写后没有改善"死循环。
    q_ind = Question(
        subject="地理", topic="产业区位", stem="测试题", max_score=2,
        points=[RubricPoint(
            seq=1, text="人才储备充足：高校培养大量电子信息专业人才，"
                        "为产业发展提供人力资源支持", score=2)],
    )
    plan_ind = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                           hit=[], partial=[1], off=[], blank=[])
    cond_only = "1. 该地高校培养了大量电子信息专业毕业生，对当地产业发展有一定帮助。"
    v_ind = find_violations(q_ind, plan_ind, cond_only)
    check("产业题照实写长名词条件 → 不算照抄细则（只跟因果链比）",
          v_ind == [], str(v_ind))
    chain_copy = ("1. 该地高校培养了大量电子信息专业人才，"
                  "为产业发展提供人力资源支持。")
    v_ind2 = find_violations(q_ind, plan_ind, chain_copy)
    check("整段抄因果链仍然抓得住",
          any("第1点" in x for x in v_ind2), str(v_ind2))

    q_off = Question(
        subject="地理", topic="测试", stem="测试题", max_score=2,
        points=[RubricPoint(seq=1, text="流域降水丰富径流量大", score=2)],
    )
    plan_off3 = StudentPlan(seq=1, ability="中等", style="浮于表面型",
                            hit=[], partial=[], off=[1], blank=[])
    mild = "1. 该地降水较多。"
    check("默认阈值下这句不算答到点上（对照用）",
          find_violations(q_off, plan_off3, mild) == [],
          str(find_violations(q_off, plan_off3, mild)))
    strict = {False: 0.05, "blank": 0.30, "partial": 0.75}
    check("用布尔键 off 传进来的阈值确实生效了",
          find_violations(q_off, plan_off3, mild, thresholds=strict) != [],
          str(find_violations(q_off, plan_off3, mild, thresholds=strict)))
    strict2 = {"off": 0.05}
    check("用字符串键 off 传进来同样生效",
          find_violations(q_off, plan_off3, mild, thresholds=strict2) != [])


def test_prompts():
    print("\n[提示词]")
    for name in ["question.txt", "answer_system.txt", "answer_user.txt",
                 "answer_fix.txt", "grading_system.txt", "grading_user.txt"]:
        text = load_prompt(name)
        check(f"{name} 能读取且非空", len(text) > 50)

    tpl = "学科：{subject}，题目：{question}，JSON：{\"a\": 1}"
    out = fill(tpl, subject="地理", question="测试")
    check("占位符被替换", "{subject}" not in out and "地理" in out)
    check("JSON 花括号不被破坏", '{"a": 1}' in out)

    # ---- 生成侧的关键约束必须留在提示词里（防止以后被改回去）----
    sys_prompt = load_prompt("answer_system.txt")
    user_prompt = load_prompt("answer_user.txt")

    # 【最重要】生成答卷时不许把评分细则发给 AI（考生视角）
    check("答卷提示词写明'你没有标准答案'",
          "没有标准答案" in sys_prompt
          or "没有人会把评分细则给你" in sys_prompt)
    check("答卷提示词禁止罗列要点凑答案",
          "罗列" in sys_prompt and "猜测命题人" in sys_prompt)
    check("答卷提示词禁止把评分细则当输入（模板里不许有 rubric 占位符）",
          "{rubric}" not in sys_prompt and "{rubric}" not in user_prompt)

    # 水平靠角色提示词控制
    check("答卷提示词写明水平靠角色提示词控制",
          "水平和答题习惯" in sys_prompt or "角色设定" in user_prompt)
    check("答卷提示词列出五个水平档（优秀/良好/中等/薄弱/很差）",
          all(k in sys_prompt for k in ("优秀", "良好", "中等", "薄弱", "很差")))
    check("答卷提示词要求各档写得明显不同",
          "必须能看出明显的水平差" in sys_prompt)
    check("答卷提示词允许的三类错误（答非所问／因果颠倒／张冠李戴）写清了",
          all(k in sys_prompt for k in ("答非所问", "因果颠倒", "张冠李戴")),
          "缺：" + "、".join(k for k in ("答非所问", "因果颠倒", "张冠李戴")
                           if k not in sys_prompt))
    check("答卷提示词要求错误'听着有道理'、不许写低级蠢话",
          "海拔越高气温越高" in sys_prompt and "听着有道理" in sys_prompt)
    # ★ 2026-10-05 用户新设计：所有档位都写满，差生也不例外。
    check("答卷提示词明令五档都要写满（不许少写几条）",
          "五档都必须写满" in sys_prompt or "一律按系统给的条数写满" in sys_prompt)
    check("答卷提示词把'卷面比优等生短'列为写砸方式",
          "卷面明显比优等生短" in sys_prompt
          or "明显比别人短" in sys_prompt)

    # 答题规范
    check("答卷提示词要求分点编号 + 完整句子（防残句）",
          "分点" in sys_prompt and "残句" in sys_prompt)
    check("答卷提示词给出书面语禁用词表",
          "我觉得" in sys_prompt and "差不多" in sys_prompt)
    check("答卷提示词写明必须咬住题目问的方向",
          "咬住题目问的方向" in sys_prompt)
    check("答卷提示词禁止跑方向的两类词",
          "经济、人口" in sys_prompt and "限制、制约" in sys_prompt)

    # 学生块（user 模板）
    check("答卷 user 模板给了题目与学生块两个占位符",
          "{question}" in user_prompt and "{student_block}" in user_prompt)
    check("答卷 user 模板说明只写一位学生",
          "{count}" not in user_prompt)

    q_prompt = load_prompt("question.txt")
    check("出题提示词要求采分点写成'条件／现象：因果机制、落脚结论'",
          "条件／现象：因果机制、落脚结论" in q_prompt)
    check("出题提示词按广东高考真题风格出题",
          "广东高考" in q_prompt and "广东卷" in q_prompt)
    check("出题提示词要求区域落到真实的省县市（小切口深分析）",
          "真实的省" in q_prompt)
    check("出题提示词禁止拿'总结性标签'当前半段",
          "总结性的标签" in q_prompt)
    check("出题提示词要求高考难度的情境材料",
          "【材料】" in q_prompt and "高考" in q_prompt)
    check("出题提示词要求多给候补采分点",
          "候补采分点" in q_prompt and "{point_total}" in q_prompt)
    check("出题提示词写明满分不等于采分点合计",
          "不要写成所有采分点分值之和" in q_prompt)

    grad = load_prompt("grading_system.txt")
    # 【2026-10-05 用户改判定标准】据第52 份批改记录实测出的三个 bug：
    #   ① 一句话重复踩两个采分点被重复给分（8 份里6 份中招）
    #   ② 缺因果连接词就只给一半分 → 制造虚假的"半吊子"
    #   ③ 差生说大白话（生成端，另在 test_answer_flow 守）
    # 三档（满分/一半/零分）已废除，收敛为"满分/零分"两档。
    check("阅卷提示词把「看到核心词就给分」列为头号禁令",
          "看到无关名词就给分" in grad and "严禁" in grad)
    check("阅卷提示词写明'核心采分点要对照本条的评分细则'",
          "核心采分点" in grad and "对照本条评分细则" in grad)

    # ★ 修复①：严禁一句话重复踩两个点（死命令）
    check("阅卷提示词有'严禁一句话重复踩两个点'的死命令",
          "严禁" in grad and "一句话重复踩两个点" in grad)
    check("阅卷提示词写明同一句话只能判给一个采分点",
          "同一句话只能判给一个采分点" in grad)
    check("阅卷提示词禁止两条采分点用同一句evidence",
          "同一个 evidence 字符串绝对不许出现在两条" in grad)
    check("阅卷提示词举了实测的重复给分错例（地理位置+交通便利）",
          "重复给分" in grad and ("物流运输" in grad or "交通便利" in grad))
    check("阅卷提示词保留了'一句话确实讲两件事才能拆开'的例外",
          "两件事写在一句里" in grad or "这是**两件事**" in grad)

    # ★★ 2026-10-05 用户新设计：三档恢复，但 1 分只对应"该分析而没写"
    check("阅卷提示词恢复三档（满分/一半/零分）",
          "三档" in grad and "满分" in grad and "一半" in grad)
    check("阅卷提示词写清 1 分只用于'本条要分析却只写了现象'",
          "只写了现象" in grad and "一半分" in grad and "1 分" in grad,
          "缺：" + "、".join(k for k in ("只写了现象", "一半分", "1 分")
                          if k not in grad))
    check("阅卷提示词要求判1 分前先看清细则有没有冒号",
          "冒号" in grad and ("没有冒号" in grad or "本身就没有冒号" in grad))
    check("阅卷提示词明令'连接词本身不扣分'（与'分析没写'分开）",
          "连接词" in grad and ("不作为扣分" in grad or "不是得分点" in grad))
    check("阅卷提示词禁止拿 level=1 惩罚'句子简短/没连接词'",
          "缺连接词" in grad and ("满分" in grad))
    check("阅卷提示词写明答非所问判0 分（不是 1 分）",
          "答非所问" in grad and "0 分" in grad)
    check("阅卷提示词要求判 1 分时写明漏掉了哪一层分析",
          "漏掉" in grad and "分析" in grad)
    check("阅卷提示词写明'电商企业数量很多'这类无冒号细则应给满分",
          "电商企业数量很多" in grad and "满分" in grad)
    check("阅卷提示词明确禁止造出虚假的半吊子分数",
          "半吊子" in grad)
    check("阅卷提示词要求同义近义表述一律算命中（不死抠字面）",
          "同义" in grad and "都算命中" in grad)
    check("阅卷提示词写明复述材料不扣分",
          "复述材料**不扣分**" in grad or "复述材料不扣分" in grad)
    check("阅卷提示词要求引用时用「」（JSON 安全）",
          "严禁在内容里使用英文双引号" in grad and "不要输出总分" in grad)

    fix = load_prompt("answer_fix.txt")
    check("改写提示词用单层花括号（AI 会照抄双层导致 JSON 解析失败）",
          '{"answer"' in fix and '{{"answer"' not in fix)
    check("改写提示词写明只修规范问题、不补内容",
          "不要试图把答案写" in fix or "不要在内容上拔高" in fix)
    check("改写提示词同样不许拿评分细则当输入",
          "{rubric}" not in fix and "系统没有给你评分标准" in fix)
    check("改写提示词包含跑方向的改法",
          "跑方向" in fix and "整句删掉" in fix)


def test_json_repair():
    """守住"AI 在 JSON 字符串里塞英文双引号"这条最坑的坏法。"""
    print("\n[JSON 容错解析]")
    from services.llm import extract_json

    check("正常 JSON 照常解析", extract_json('{"a": 1}')["a"] == 1)
    check("带 ```json 围栏的能解析",
          extract_json('```json\n{"a": 2}\n```')["a"] == 2)
    check("空字符串字段不被误改",
          extract_json('{"a": "", "b": "x"}')["a"] == "")

    # 实测踩过：AI 引用核心定语时用了英文双引号，把 JSON 从中间切断
    broken = ('{"points": [{"index": 1, "level": 1, '
              '"reason": "缺少"地势相对平坦开阔"的核心定语", '
              '"evidence": "该地地形条件有利于农业生产"}], "comment": "一般"}')
    parsed = extract_json(broken)
    check("字符串里混进英文双引号也能救回来",
          parsed["points"][0]["level"] == 1, str(parsed)[:140])
    check("救回来之后引号里的内容还在",
          "地势相对平坦开阔" in parsed["points"][0]["reason"],
          parsed["points"][0]["reason"])
    check("后面的字段没有被吞掉",
          parsed["points"][0]["evidence"] == "该地地形条件有利于农业生产",
          parsed["points"][0]["evidence"])


def test_config_matches_code():
    """守住"配置里的名字必须和代码里的一致"这条。

    实测踩过的坑：方向修正时把画像从"答非所问型为主"改成了"核心词残缺型为主"，
    代码改了、config.yaml 忘了改。旧名字在代码里认不出来，被静默丢掉，
    结果半对卷 100% 都成了答非所问型——配置白填，而且没人发现。
    这一轮又改了一次画像名（核心词残缺型 → 浮于表面型），
    所以这里把配置和代码逐项对齐，以后改名字会立刻报警。
    """
    print("\n[配置与代码一致性]")
    from core.config import load_config
    from core.planner import (
        DEFAULT_STYLE_WEIGHTS, HALF_STYLES, LENGTH_SPEC, TARGET_RATIO,
    )

    cfg = load_config()
    cls = cfg.get("classroom", {})

    sw = cls.get("style_weights") or {}
    check("config.yaml 的 style_weights 不是空的", bool(sw))
    bad = [k for k in sw if k not in HALF_STYLES]
    check("style_weights 里没有代码不认识的画像名（改名字会导致配置失效）",
          not bad, f"不认识：{bad}；合法名字：{list(HALF_STYLES)}")
    check("五类半吊子画像在配置里都给了比例",
          set(sw) == set(HALF_STYLES), str(sorted(sw)))
    check("配置比例加起来约等于 1",
          abs(sum(float(v) for v in sw.values()) - 1.0) < 0.02,
          f"{sum(float(v) for v in sw.values()):.2f}")
    check("主力画像（浮于表面型）占比不低于 30%",
          float(sw.get("浮于表面型", 0)) >= 0.30,
          str(sw.get("浮于表面型")))
    check("似是而非型占比不低于 20%（真实考生'假会'的主线之一）",
          float(sw.get("似是而非型", 0)) >= 0.20,
          str(sw.get("似是而非型")))

    aw = cls.get("ability_weights") or {}
    bad_ab = [k for k in aw if k not in TARGET_RATIO]
    check("ability_weights 里的档位名代码都认识", not bad_ab, str(bad_ab))
    check("五个档位都配了比例", set(aw) == set(TARGET_RATIO), str(sorted(aw)))
    half = sum(float(aw.get(k, 0)) for k in ("良好", "中等", "薄弱"))
    check("'半对'三档合计约 70%（这是正确率被按住的关键）",
          0.60 <= half <= 0.80, f"{half:.0%}")

    check("LENGTH_SPEC 覆盖了全部档位", set(LENGTH_SPEC) == set(TARGET_RATIO))

    # ★★ 2026-10-05 用户新设计：**条数一律放开，靠"故意写错"压分**
    # 旧规则是"下两档明显写得少"（靠篇幅差撑区分度）。
    # 用户明确要求"所有档位都写满，差生也写 4~5 条，跟优等生一样多"——
    # 因为砍条数虽然能压分，但差生的卷面一眼就看出比优等生短，
    # 教师练批改时会先注意到长短而不是内容对错。
    # 现在靠 planner.WRONG_QUOTA 的错误配额压分（见下面的断言）。
    check("中上三档写满（字数下限 ≥150）",
          all(LENGTH_SPEC[a][2] >= 150 for a in ("优秀", "良好", "中等")),
          str({a: LENGTH_SPEC[a][2] for a in ("优秀", "良好", "中等")}))
    check("★下两档也要写满条数（用户要求差生卷面不更短）",
          LENGTH_SPEC["薄弱"][0] >= 4 and LENGTH_SPEC["很差"][0] >= 4,
          f"薄弱{LENGTH_SPEC['薄弱'][0]}／很差{LENGTH_SPEC['很差'][0]}")
    check("★下两档的字数也要跟上（不能一眼看出长短）",
          LENGTH_SPEC["很差"][2] >= 100 and LENGTH_SPEC["薄弱"][2] >= 100,
          f"很差下限{LENGTH_SPEC['很差'][2]}／薄弱下限{LENGTH_SPEC['薄弱'][2]}")
    check("最差档也不是交白卷（至少 2 条、60 字）",
          LENGTH_SPEC["很差"][0] >= 2 and LENGTH_SPEC["很差"][2] >= 60,
          str(LENGTH_SPEC["很差"]))
    check("配置里有'篇幅均衡'开关（现在只防极端残卷）",
          bool(cls.get("length_balance", True)))

    from core.planner import (
        ROLE_PROMPTS, WRONG_KINDS, WRONG_QUOTA, WRONG_SAMPLES_BY_FIELD,
        pick_wrong_kinds,
        student_role, wrong_quota,
    )
    check("薄弱／很差档的角色提示词都强调答题卡不许空着",
          all(("空着" in ROLE_PROMPTS.get(a, "") or "写满" in ROLE_PROMPTS.get(a, ""))
              for a in ("薄弱", "很差")),
          str({a: ROLE_PROMPTS.get(a, "")[:24] for a in ("薄弱", "很差")}))
    # ★ 2026-10-05：三档的"写得浅"描述不许再说"少写几条"——
    #   篇幅由 LENGTH_SPEC 管，水平差异靠错误类型与"推不推得深"。
    check("★五档角色提示词都不许出现'少写/停笔/写不出'这类篇幅指令",
          not any(kw in ROLE_PROMPTS.get(a, "")
                  for a in ("优秀", "良好", "中等", "薄弱", "很差")
                  for kw in ("少写", "停笔", "想不起来了", "几行就停")),
          str({a: kw for a in ("中等", "薄弱", "很差")
               for kw in ("少写", "停笔", "想不起来了")
               if kw in ROLE_PROMPTS.get(a, "")}))
    check("★中等档要求'不往下推'（阅卷端据此给 1 分档）",
          ("不擅长" in ROLE_PROMPTS.get("中等", "")
           or "不往下推" in ROLE_PROMPTS.get("中等", "")),
          ROLE_PROMPTS.get("中等", "")[-40:])

    # ★★ 错误配额：这是压得分率的主力阀门，必须守住
    check("WRONG_QUOTA 覆盖了全部档位",
          set(WRONG_QUOTA) == set(TARGET_RATIO),
          str(sorted(set(TARGET_RATIO) - set(WRONG_QUOTA))))
    check("★优秀档一条错都不许有", wrong_quota("优秀") == (0, 0),
          str(wrong_quota("优秀")))
    check("★错误配额随水平递减（中等 1~2／薄弱 2~3／很差 2~3）",
          wrong_quota("中等") == (1, 2)
          and wrong_quota("薄弱") == (2, 3)
          and wrong_quota("很差") == (2, 3),
          str({a: wrong_quota(a) for a in ("中等", "薄弱", "很差")}))
    check("★优秀/良好档错误配额明显少于下两档（错误是主力阀门）",
          wrong_quota("优秀")[1] < wrong_quota("中等")[0]
          and wrong_quota("良好")[1] <= wrong_quota("中等")[0],
          str({a: wrong_quota(a) for a in ("优秀", "良好", "中等")}))
    check("★三类错误名称与生成提示词里写的完全一致",
          all(k in load_prompt("answer_system.txt") for k in WRONG_KINDS),
          "缺：" + "、".join(k for k in WRONG_KINDS
                          if k not in load_prompt("answer_system.txt")))
    # 抽出来的必须是表里有的名字，且条数落在配额区间内
    import random as _r
    ok_n, ok_k = True, True
    for a in WRONG_QUOTA:
        lo, hi = wrong_quota(a)
        for _ in range(30):
            kinds = pick_wrong_kinds(a, _r.Random())
            if not (lo <= len(kinds) <= hi):
                ok_n = False
            if any(k not in WRONG_KINDS for k in kinds):
                ok_k = False
    check("★抽出来的错误条数都落在该档配额内", ok_n,
          f"中等={wrong_quota('中等')} 抽到 {[len(pick_wrong_kinds('中等', _r.Random())) for _ in range(5)]}")
    check("★抽出来的错误名称都在三类之内", ok_k, str(WRONG_KINDS))
    # 角色提示词里要真的带上了错误配额，且是**点名条号**的硬指令
    check("★student_role 会把错误配额写进角色提示词",
          "点名改错" in student_role("很差", "", ["答非所问", "因果颠倒"], 5),
          student_role("很差", "", ["答非所问"], 5)[:80])
    # ★ 实测：抽象的"你会答错"AI 不听，必须点名到第几条
    import re as _re
    _slots = [int(x) for x in _re.findall(r"第 (\d+) 条写成",
                student_role("很差", "", ["因果颠倒", "答非所问"], 9))]
    check("★错误指令点到了具体条号（实测：抽象指令 AI 会忽略）",
          len(_slots) == 2 and _slots == sorted(_slots),
          f"实际点了第 {_slots} 条")
    # 错误条号不能落在第 1 条（那条写得最像样，写错代价太大），
    # 也不许全挤在末尾（那样 AI 会只把最后一条写错）
    _slots3 = [int(x) for x in _re.findall(r"第 (\d+) 条写成",
                student_role("很差", "", ["答非所问", "因果颠倒", "张冠李戴"], 9))]
    check("★错误条号从第 2 条起（不占掉最像样的第一条）",
          bool(_slots3) and 1 not in _slots3, f"实际点了第 {_slots3} 条")
    check("★错误条号散开铺，不挤在末尾",
          bool(_slots3) and _slots3[0] <= 5 and _slots3[-1] >= 8,
          f"9 条的卷子里点了第 {_slots3} 条")
    # ★ 错误条数不能占满：至少一半条目是对的，否则不像真实考生
    _slots_s = [int(x) for x in _re.findall(r"第 (\d+) 条写成",
                 student_role("很差", "", ["答非所问", "因果颠倒", "张冠李戴"], 4))]
    check("★错误条数不超过总条数的 1/3（错句不能占满卷面）",
          len(_slots_s) <= max(1, 4 // 3),
          f"4 条里点了 {len(_slots_s)} 条：{_slots_s}")
    check("★每类错误在每个领域都带可模仿的改写示例（只给名字 AI 抓不住）",
          set(WRONG_SAMPLES_BY_FIELD) == {"人文", "自然"}
          and all(set(v) == set(WRONG_KINDS)
                  and all(s and len(s) > 20 for s in v.values())
                  for v in WRONG_SAMPLES_BY_FIELD.values()),
          str({k: sorted(v) for k, v in WRONG_SAMPLES_BY_FIELD.items()}))
    check("★错误指令强调不许犯低级蠢话",
          "海拔越高" in student_role("很差", "", ["答非所问"], 5))
    check("★优秀档的角色提示词里没有'点名改错'",
          "点名改错" not in student_role("优秀", "扎实型", []))

    check("默认画像比例也是五类齐全",
          set(DEFAULT_STYLE_WEIGHTS) == set(HALF_STYLES))

    check("answer_batch 必须是 1（连写多份会让 AI 照抄评分标准）",
          int(cls.get("answer_batch", 0)) == 1, str(cls.get("answer_batch")))
    gate = cls.get("similarity_thresholds") or {}
    check("similarity_thresholds 三个键齐全",
          set(gate) >= {"blank", "off", "partial"}, str(sorted(gate, key=str)))
    check("similarity_thresholds 的键都是字符串（off 必须加引号，否则被 YAML 读成布尔）",
          all(isinstance(k, str) for k in gate), str([type(k).__name__ for k in gate]))
    mc = cls.get("min_common_chars") or {}
    check("min_common_chars 三个键齐全",
          set(mc) >= {"blank", "off", "partial"}, str(sorted(mc, key=str)))
    check("min_common_chars 的键都是字符串（off 必须加引号）",
          all(isinstance(k, str) for k in mc), str([type(k).__name__ for k in mc]))

    # 智谱的硬约束：温度只能 0~1（超了会被接口拒绝）
    ai = cfg.get("ai", {})
    check("温度上限不超过 1（智谱限制）",
          float(ai.get("temperature_max", 1.0)) <= 1.0,
          str(ai.get("temperature_max")))
    check("出题温度不超过 1", float(cfg["question"].get("temperature", 0)) <= 1.0)

    # 【2026-10-02 用户要求】之前连着出的都是人文大题，自然类必须补上。
    # 靠"记住上次出的类型、这次换一类"来保证，光靠提示词列主题清单不管用。
    qcfg = cfg.get("question", {})
    check("config 里有出题类型开关（auto=自然/人文交替）",
          str(qcfg.get("topic_type", "auto")).lower()
          in ("auto", "natural", "human"),
          str(qcfg.get("topic_type")))
    from services.question_service import pick_topic_type
    check("auto 模式与上一次相反（交替出题，不会连着出人文题）",
          pick_topic_type({"question": {"topic_type": "auto"}}, "human") == "natural"
          and pick_topic_type({"question": {"topic_type": "auto"}}, "natural") == "human")
    check("第一次运行（还没有记录）时优先出自然题",
          pick_topic_type({"question": {"topic_type": "auto"}}, "") == "natural")
    check("显式指定 natural / human 时按指定来",
          pick_topic_type({"question": {"topic_type": "natural"}}, "human") == "natural"
          and pick_topic_type({"question": {"topic_type": "human"}}, "natural") == "human")
    check("答卷温度不超过 1",
          float(cls.get("answer_temperature", 0)) <= 1.0)
    check("阅卷温度必须是 0（冻结随机性）",
          float(cfg.get("grading", {}).get("temperature", -1)) == 0.0)
    check("阅卷重复次数 >= 3（靠投票压住抖动）",
          int(cfg.get("grading", {}).get("repeats", 0)) >= 3)

    # 端到端：直接拿 config.yaml 里的比例跑一遍，看主力画像到底是谁。
    # 名字一旦对不上，这里就会从"浮于表面型"变成默认比例。
    from collections import Counter
    from core.planner import make_plans
    q = Question(
        subject="地理", topic="测试", stem="测试题", max_score=8,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 5)],
    )
    plans = make_plans(
        q, 400, aw,
        blank_rate=float(cls.get("blank_rate", 0.0)),
        style_weights=sw,
    )
    half_rows = [p for p in plans if p.ability in ("良好", "中等", "薄弱")]
    dist = Counter(p.style for p in half_rows)
    top = dist.most_common(1)[0][0] if dist else ""
    check("按 config.yaml 的比例跑，半对卷主力画像是'浮于表面型'",
          top == "浮于表面型", str(dict(dist)))
    check("半对卷主力画像占比不低于 30%（期望值 0.35，留出抽样波动余地）",
          dist.get("浮于表面型", 0) / max(1, len(half_rows)) >= 0.30,
          f"{dist.get('浮于表面型', 0) / max(1, len(half_rows)):.0%}")
    check("半对卷里不再出现'答非所问型'",
          dist.get("答非所问型", 0) == 0, str(dict(dist)))

    # ---- 方向红线的配置 ----
    check("direction_guard 默认打开", bool(cls.get("direction_guard", False)))
    fw = cls.get("forbidden_words") or {}
    check("forbidden_words 两张表齐全（natural / favorable）",
          set(fw) >= {"natural", "favorable"}, str(sorted(fw)))
    check("natural 表里有'经济'和'人口'（用户点名的跑题词）",
          "经济" in (fw.get("natural") or []) and "人口" in (fw.get("natural") or []),
          str(fw.get("natural")))
    check("favorable 表里有'崎岖'和'制约'（用户点名的负面词）",
          "崎岖" in (fw.get("favorable") or [])
          and "制约" in (fw.get("favorable") or []),
          str(fw.get("favorable")))

    # ---- 候补采分点 ----
    check("spare_points 是整数且 >= 0",
          isinstance(cfg["question"].get("spare_points"), int)
          and int(cfg["question"].get("spare_points", -1)) >= 0,
          str(cfg["question"].get("spare_points")))
    check("point_score 能整除分值候选（否则点数除不尽）",
          all(float(o) % float(cfg["question"].get("point_score", 2)) == 0
              for o in cfg["question"].get("score_options", [])),
          str(cfg["question"].get("score_options")))


def test_score_cap():
    """守住"多答不加分"这条：候补采分点全答对，总分也只能是满分。"""
    print("\n[总分封顶]")
    from core.aggregator import aggregate

    # 8 分的题配了 5 个 2 分点（多出来的是候补），合计 10 分
    points = [RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 6)]
    runs = [GradingRun(0, [
        PointVerdict(i, 2, "链条完整", f"原句{i}") for i in range(1, 6)
    ])]
    capped = aggregate(1, points, runs, max_score=8)
    check("5 个点全对、满分 8 → 判 8 分（不是 10 分）",
          capped.total == 8.0, str(capped.total))

    uncapped = aggregate(2, points, runs)
    check("不传满分时退回按合计算（兼容旧用法）",
          uncapped.total == 10.0, str(uncapped.total))

    # 各点分值不等也要封得住
    p3 = [RubricPoint(seq=1, text="甲", score=4), RubricPoint(seq=2, text="乙", score=4)]
    r3 = [GradingRun(0, [PointVerdict(1, 2, "好", "甲"), PointVerdict(2, 2, "好", "乙")])]
    check("合计 8 分、满分 6 → 判 6 分",
          aggregate(3, p3, r3, max_score=6).total == 6.0,
          str(aggregate(3, p3, r3, max_score=6).total))
    check("封顶后多次调用的波动也按封顶算",
          aggregate(4, points, runs, max_score=8).score_range == 0.0)


def test_question_normalize():
    """守住"满分来自设问、不来自采分点合计"这条（候补采分点靠它成立）。"""
    print("\n[出题解析 · 候补采分点]")
    from services.question_service import _normalize

    data = {
        "subject": "地理",
        "topic": "农业",
        "question": "【材料】……\n【设问】分析该地种植青稞的有利自然条件。（8分）",
        "max_score": 8,
        "points": [
            {"text": "昼夜温差大：白天光合作用强", "score": 2},
            {"text": "海拔高：生长周期长", "score": 2},
            {"text": "降水集中：满足需水期", "score": 2},
            {"text": "日照充足：光合作用强", "score": 2},
            {"text": "土壤肥沃：养分充足", "score": 2},
        ],
    }
    q = _normalize(data, "地理", 2.0)
    check("满分保持 8（不等于采分点合计 10）", q.max_score == 8.0, str(q.max_score))
    check("候补采分点被保留下来（共 5 条）", len(q.points) == 5, str(len(q.points)))
    check("采分点分值都是 2", all(p.score == 2 for p in q.points))

    # AI 忘记给 max_score → 从设问里的"（8分）"兜底抽出来
    data2 = dict(data)
    data2.pop("max_score")
    q2 = _normalize(data2, "地理", 2.0)
    check("max_score 缺失时能从设问里抽出 8 分", q2.max_score == 8.0, str(q2.max_score))

    # 设问里也没写分值 → 退回按合计算（旧行为）
    data3 = dict(data)
    data3.pop("max_score")
    data3["question"] = "【设问】分析该地的有利自然条件。"
    q3 = _normalize(data3, "地理", 2.0)
    check("都没写时退回按合计算", q3.max_score == 10.0, str(q3.max_score))


def test_spare_points_do_not_inflate():
    """候补采分点不能变成"额外的得分机会"。

    实测踩过：8 分的题配了 5 个 2 分点（合计 10），目标分却仍按"合计"算，
    于是良好档的目标变成 6.5 分，而他只要把 5 个点里的 4 个写全就正好 8 分满分——
    全班"优秀+良好"一起满分。目标分必须按【题面满分】算。
    """
    print("\n[候补采分点不抬高得分]")
    q5 = Question(
        subject="地理", topic="测试", stem="测试题", max_score=8,
        points=[RubricPoint(seq=i, text=f"点{i}", score=2) for i in range(1, 6)],
    )
    weights = {"优秀": 0.10, "良好": 0.20, "中等": 0.30, "薄弱": 0.20, "很差": 0.20}
    plans = make_plans(q5, 600, weights, blank_rate=0.15)

    check("设计分一律不超过题面满分",
          all(p.design_score <= 8.0 + 1e-6 for p in plans),
          str(max(p.design_score for p in plans)))

    good = [p for p in plans if p.ability == "良好"]
    g_hit = sum(len(p.hit) for p in good) / max(1, len(good))
    g_design = sum(p.design_score for p in good) / max(1, len(good))
    check("良好档不会因为多了一个点就凑到满分（设计分 < 7）",
          g_design < 7.0, f"{g_design:.2f}")
    check("良好档 5 个点里完整命中的不超过 3 个", g_hit <= 3.2, f"{g_hit:.2f}")

    mid = [p for p in plans if p.ability == "中等"]
    m_hit = sum(len(p.hit) for p in mid) / max(1, len(mid))
    m_part = sum(len(p.partial) for p in mid) / max(1, len(mid))
    check("中等档在 5 个点的题里仍是'完整 + 部分 + 未命中'的混合",
          m_hit >= 0.6 and m_part >= 1.5, f"完整 {m_hit:.2f} / 部分 {m_part:.2f}")
    # 抽点：有候补点时，谁都不许把 5 个点全写
    drawn = {len(p.hit) + len(p.partial) + len(p.off) for p in plans}
    check("有候补点时没人把 5 个点全写", 5 not in drawn, str(sorted(drawn)))


def test_material_leak_guard():
    """出题端的"答案泄漏"闸门：材料里写了做法 → 必须能检出。

    实测踩过（第 28 号存档）：材料里写着"东莞政府在此期间出台了产业转型升级
    政策，引导传统制造业向粤北、粤西等地区转移，并在本地大力发展智能制造、
    电子信息等新兴产业"，而评分细则第 1/2 点的因果链就是这句话——
    学生照抄材料就命中，第 1 份直接拿了 8 分满分、整班答案雷同。
    """
    print("\n[出题 · 答案泄漏闸门]")
    from services.question_service import LEAK_MIN_RUN, rubric_leak_problems

    leaked = Question(
        subject="地理", topic="产业转移", max_score=8,
        stem=(
            "【材料】东莞市位于珠江口东岸。2010—2020年间，东莞规模以上工业企业"
            "数量减少了约35%，但高新技术产业产值占GDP比重由15%升至42%。"
            "东莞政府在此期间出台了产业转型升级政策，引导传统制造业向粤北、"
            "粤西等地区转移，并在本地大力发展智能制造、电子信息等新兴产业。\n"
            "【设问】分析东莞市2010—2020年间产业结构变化的特点及其原因。（8分）"
        ),
        points=[
            RubricPoint(
                seq=1,
                text="传统制造业比重下降：传统制造业向粤北、粤西等地区转移，"
                     "导致本地企业数量减少",
                score=2,
            ),
            RubricPoint(
                seq=2,
                text="高新技术产业比重上升：政府引导发展智能制造、电子信息等"
                     "新兴产业，企业数量和产值大幅增长",
                score=2,
            ),
            RubricPoint(
                seq=5,
                text="产业政策引导：政府出台产业转型升级政策，"
                     "推动传统产业转移和新兴产业发展",
                score=2,
            ),
        ],
    )
    probs = rubric_leak_problems(leaked)
    check("材料里写了'做法'时能检出答案泄漏", len(probs) >= 2,
          f"检出 {len(probs)} 条：{probs}")

    clean = Question(
        subject="地理", topic="产业转移", max_score=8,
        stem=(
            "【材料】东莞市位于珠江口东岸。2010—2020年间，东莞规模以上工业企业"
            "数量减少了约35%，但高新技术产业产值占GDP比重由15%升至42%；"
            "制造业从业人员数量减少约20%，而从业人员平均工资增长约85%。\n"
            "【设问】分析东莞市2010—2020年间产业结构变化的原因。（8分）"
        ),
        points=[
            RubricPoint(seq=1,
                        text="劳动力成本上升：低端制造业利润被压缩，"
                             "劳动密集型企业被迫向外转移", score=2),
            RubricPoint(seq=2,
                        text="就业结构变化：产业升级对高技能人才需求增加，"
                             "从业人员减少但工资水平上升", score=2),
        ],
    )
    clean_probs = rubric_leak_problems(clean)
    check("材料只给数据时不误报", clean_probs == [], str(clean_probs))
    check("阈值是 10 字（专有名词撞车不会误伤）", LEAK_MIN_RUN == 10, str(LEAK_MIN_RUN))


def test_material_reaches_everyone():
    """★ 题库来的题，材料必须送到每一个需要它的人手里。

    背景（2026-10-04 用户验收时抓出来的坑）：
      题库 txt 里"材料"和"设问"是分开两段的，材料存在 qbank_files 里，
      不在 stem 里。而生成答卷、阅卷、教师盲评、Excel 导出全都只发 stem ——
      于是【学生看不到材料】（凭空编答案）、【阅卷官看不到材料】
      （判分依据"条件／现象有没有点出来"直接失效）、【教师看不到材料】。
      修法是这些地方一律改用 full_stem()。

    这条测试就是防止将来有人"顺手"改回question.stem。
    """
    print("\n[题库 · 材料送达全链路]")

    q = Question(
        subject="地理", topic="地表形态的塑造", max_score=6,
        material="黑排角岩滩位于广东省东部海岸带，基岩岩性为流纹岩。",
        stem="推测流纹岩的形成过程。",
        points=[RubricPoint(seq=1, text="岩浆沿断裂带喷出地表", score=2)],
    )
    full = q.full_stem()
    check("full_stem() = 材料 + 设问",
          full == q.material + "\n\n" + q.stem, repr(full))
    check("full_stem() 里有材料", "流纹岩" in full, repr(full[:40]))

    # AI 随机出题时代的老存档：material 为空，full_stem() 必须原样返回 stem
    old = Question(
        subject="地理", topic="测试", max_score=8, stem="【材料】某河段位于湿润山区。",
        points=[RubricPoint(seq=1, text="降水丰富", score=2)],
    )
    check("老存档（material 为空）full_stem() == stem，不受影响",
          old.full_stem() == old.stem, repr(old.full_stem()))

    # 真正发提示词的地方，必须用的是 full_stem() 而不是 stem
    import inspect

    from services import answer_service, grading_service
    for mod, fn in ((answer_service, "_gen_one"),
                    (answer_service, "_fix_answer"),
                    (grading_service, "_build_prompts")):
        src = inspect.getsource(getattr(mod, fn))
        check(f"{mod.__name__.split('.')[-1]}.{fn}() 用 full_stem() 发题面",
              "full_stem()" in src and "question.stem" not in src,
              "还在用裸 stem")

    # 界面和导出
    from app.screens import grading as grading_ui
    from storage import export as export_mod
    check("教师盲评页用 full_stem() 显示题面",
          "full_stem()" in inspect.getsource(grading_ui._question_small),
          "还在用裸 stem")
    check("Excel 导出用 full_stem()", "full_stem()" in inspect.getsource(export_mod),
          "还在用裸 stem")

    # 答案泄漏闸门要读 material（题库题的材料不在 stem 里）
    from services.question_service import _material_of
    check("答案泄漏闸门优先读 material",
          _material_of(q) == q.material, repr(_material_of(q)))
    check("老题仍从 stem 里切材料",
          _material_of(old) == "【材料】某河段位于湿润山区。", repr(_material_of(old)))


def test_profile_library():
    """考生档案库接入（2026-10-05）——守住三条易碎的东西。"""
    from core import profiles as pm
    from core.planner import WRONG_KINDS, student_role

    allp = pm.load_profiles()
    check("档案库能读到 60 份", len(allp) == 60, f"实际 {len(allp)}")

    # ---- 1. 抽样配额：抽 8 份时五档都要露面 ----
    q8 = pm.quota_for(8, allp)
    check("抽 8 份配额合计正好 8", sum(q8.values()) == 8, str(q8))
    check("抽 8 份时优秀档抽得到（纯按比例取整会把尖子生全漏掉）",
          q8.get("优秀", 0) >= 1, str(q8))
    check("抽 8 份时良好档抽得到", q8.get("良好", 0) >= 1, str(q8))
    check("中等档是抽取的主力（最多）",
          q8.get("中等", 0) == max(q8.values()), str(q8))

    # 跑 30 次，份数必须次次是 8、档位不越界
    from collections import Counter
    cnt = Counter()
    ok_n = True
    for _ in range(30):
        ps = pm.pick_profiles(8)
        if len(ps) != 8:
            ok_n = False
        for p in ps:
            cnt[p["level"]] += 1
    check("抽 30 次每次都是 8 份（不因取整而少人）", ok_n)
    check("攒 240 份里五档都出现过", set(cnt) == set(pm.LEVELS), str(sorted(cnt)))

    # ---- 2. ★ 错误类型必须跟档案声明【对齐】（不能现抽） ----
    picks = [p for p in allp if p["id"] in (22, 57)]
    check("能按 id 取到 #22 / #57", len(picks) == 2)
    plans = make_plans(None or _fake_question(), len(picks),
                       weights={}, profiles=pm.to_plan_kwargs(picks))
    for prof, plan in zip(picks, plans):
        want = [k for k in (prof.get("error_tendencies") or [])
                if k in WRONG_KINDS]
        check(f"档案 #{prof['id']} 的错误类型原样进了 plan",
              plan.wrong_kinds == want, f"{plan.wrong_kinds} vs {want}")
        check(f"档案 #{prof['id']} 的 role_hint 原样进了 plan",
              plan.role_hint == prof["role_hint"])
        check(f"档案 #{prof['id']} 的档位进了 plan",
              plan.ability == prof["level"])

    # "漏点 / 堆材料"是卷面形态，不该混进"写错的 N 条"
    loose = [p for p in allp if set(p["error_tendencies"]) & {"漏点", "堆材料"}]
    check("存在带漏点/堆材料的档案（用例有效性）", len(loose) > 0)
    lp = make_plans(_fake_question(), len(loose),
                    weights={}, profiles=pm.to_plan_kwargs(loose))
    check("漏点/堆材料没有被当成『某一条写错』塞进 wrong_kinds",
          all(k in WRONG_KINDS for plan in lp for k in plan.wrong_kinds),
          str([p.wrong_kinds for p in lp][:3]))

    # ---- 3. ★★ 接入方式必须是【叠加】：点名指令 + 人物设定 同时出现 ----
    prof = picks[1]                      # #57 很差档，错误最多，最能看出叠加
    plan = plans[1]
    role = student_role(plan.ability, plan.style, plan.wrong_kinds,
                        n_lines=5, role_hint=plan.role_hint)
    check("提示词里有档案的人设（【人物设定】段）", "【人物设定】" in role)
    check("提示词里的人设就是档案原文", plan.role_hint in role)
    check("★ 提示词里同时有点名改错的硬指令（叠加，不是二选一）",
          "点名改错" in role and "第 " in role)
    check("错误指令排在人物设定【前面】（放后面 AI 会无视）",
          role.index("点名改错") < role.index("【人物设定】"))

    # ---- 4. 不传档案时，必须完全退回旧行为 ----
    old = student_role("中等", "浮于表面型", ["张冠李戴"], n_lines=5)
    check("不传 role_hint 时仍走 ROLE_PROMPTS（老流程零影响）",
          "浮于表面型" in old and "点名改错" in old)
    plain = student_role("优秀", "", [], n_lines=5, role_hint="")
    check("无错误 + 无人设时返回纯档位提示词", "地理" in plain or len(plain) > 0)

    # ---- 5. 档案文件损坏 / 缺失时必须不崩 ----
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        check("档案文件不存在时返回空列表（不抛异常）",
              pm.load_profiles(Path(td)) == [])
        bad = Path(td) / "student_profiles.json"
        bad.write_text("{ 这不是合法 json", encoding="utf-8")
        check("档案文件损坏时返回空列表（不崩）",
              pm.load_profiles(Path(td)) == [])
        bad.write_text('{"profiles": [{"id": 1, "level": "不存在的档"}]}',
                       encoding="utf-8")
        check("档位不认识的档案被跳过",
              pm.load_profiles(Path(td)) == [])


def test_field_lock():
    """★ 学科领域锁定（2026-10-05 第 66 批）——错误不许跨科。

    背景：人文题里冒出了"亚热带季风气候""板块交界处""地形崎岖"，
    教师一眼看出"这不像差生写的，像根本没看题"。
    根因是旧的错误示例全是自然地理的，AI 照着套就串了领域。
    """
    from core.planner import (
        WRONG_KINDS, _wrong_instruction, detect_field,
        student_role,
    )

    PHY = ("气候", "季风", "地形", "崎岖", "地质", "水文", "植被",
           "土壤", "板块", "降水", "气温", "风化", "岩石", "岩浆")
    HUM = ("市场", "政策", "劳动力", "交通", "产业", "人口", "城市",
           "电商", "区位", "经济", "产业链", "物流")

    human_q = Question(
        subject="地理", topic="产业区位与区域发展（义乌电商）",
        stem="说明义乌成为该区域中心城市的主要原因。",
        material="【材料】义乌本是浙江中部山区的一个小镇，因其小商品批发……",
        max_score=8.0,
        points=[RubricPoint(seq=1, text="拥有强大的小商品集散市场", score=2.0)],
    )
    phys_q = Question(
        subject="地理", topic="地表形态的塑造（盐风化与岩石形成）",
        stem="推测流纹岩的形成过程。",
        material="【材料】黑坡角岩滩位于广东省东部海岸带，地处莲花山断裂带……",
        max_score=6.0,
        points=[RubricPoint(seq=1, text="岩浆沿断裂带喷出地表", score=2.0)],
    )

    # ---- 1. 领域识别 ----
    check("人文题被认出来", detect_field(human_q) == "人文",
          detect_field(human_q))
    check("自然题被认出来", detect_field(phys_q) == "自然",
          detect_field(phys_q))
    blank = Question(subject="地理", topic="", stem="这是什么？",
                     max_score=6.0, points=[])
    check("判不出来时返回空（不锁定，宁可宽松不误伤）",
          detect_field(blank) == "", repr(detect_field(blank)))

    # ---- 2. ★★ 核心断言：人文题的提示词里不许有自然名词 ★★ ----
    bad = []
    for kind in WRONG_KINDS:
        text = _wrong_instruction([kind], 5, "人文", False)
        hit = [w for w in PHY if w in text]
        if hit:
            bad.append((kind, hit))
    check("★人文题的三种错误指令里零自然地理名词（含否定式列举也要干净）",
          not bad, str(bad))

    bad2 = []
    for kind in WRONG_KINDS:
        text = _wrong_instruction([kind], 5, "自然", False)
        hit = [w for w in HUM if w in text]
        if hit:
            bad2.append((kind, hit))
    check("★自然题的三种错误指令里零人文地理名词",
          not bad2, str(bad2))

    # ---- 3. 有人文示例可以照着改（不能只是"禁止"） ----
    t = _wrong_instruction(["张冠李戴"], 5, "人文", False)
    check("人文题的张冠李戴给的是人文示例（市场/劳动力一类）",
          ("市场" in t or "劳动力" in t or "政策" in t))

    # ---- 4. 跨领域只对很差档的一小部分人放行 ----
    import random as _rnd
    _rnd.seed(7)
    mid_cross = sum(
        1 for _ in range(120)
        if "唯一的例外" in student_role("中等", "", ["张冠李戴"], 5,
                                        question=human_q)
    )
    check("★中等档【从不】放行跨领域（它不该串科）", mid_cross == 0,
          f"{mid_cross}/120")
    low_cross = sum(
        1 for _ in range(300)
        if "唯一的例外" in student_role("很差", "", ["张冠李戴"], 5,
                                        question=human_q)
    )
    check("很差档只有一部分人放行跨领域（设计值 30%，实测不超一半）",
          0 < low_cross < 150, f"{low_cross}/300")

    # ---- 5. 不传 question 时不锁定（老行为，测试与旧调用零影响） ----
    old = student_role("中等", "浮于表面型", ["张冠李戴"], 5)
    check("不传 question 时不做领域锁定（老流程不变）",
          "本题是【" not in old and "点名改错" in old)


def _fake_question():
    """造一道 3 个采分点的假题（不联网、不读库）。"""
    return Question(
        subject="地理",
        topic="岩石与地貌",
        stem="推测流纹岩的形成过程。",
        material="【材料】黑排角岩滩……",
        max_score=6.0,
        points=[
            RubricPoint(seq=1, text="岩浆沿断裂带喷出地表", score=2.0),
            RubricPoint(seq=2, text="迅速冷却凝固形成流纹岩", score=2.0),
            RubricPoint(seq=3, text="地壳抬升、外力侵蚀使岩石出露", score=2.0),
        ],
    )


def main() -> None:
    print("=" * 46)
    print(" 批改模拟器 · 核心逻辑自测")
    print("=" * 46)
    test_planner()
    test_draw_range()
    test_distribution()
    test_styles()
    test_aggregator()
    test_report()
    test_quality_gate()
    test_prompts()
    test_json_repair()
    test_config_matches_code()
    test_score_cap()
    test_question_normalize()
    test_spare_points_do_not_inflate()
    test_material_leak_guard()
    test_material_reaches_everyone()
    test_profile_library()
    test_field_lock()
    print("\n" + "=" * 46)
    if FAILED:
        print(f" 有 {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        sys.exit(1)
    print(" 全部通过")


if __name__ == "__main__":
    main()
