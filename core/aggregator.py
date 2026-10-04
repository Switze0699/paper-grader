"""把 AI 的多次判定汇总成一个稳定的分数。

稳定性策略：
1. 每个采分点单独投票（不整份投票），单次失误最多影响一个点
2. 投票用"众数"，多次调用里出现最多的档位胜出
3. 平局时取中间值，避免极端
4. 汇总规则（按分值加权求和 + 封顶）写死在这里，不经过 AI，保证可复现

【总分封顶 · 为什么需要】
出题时会故意多给一两个"候补采分点"（config.yaml 的 question.spare_points）：
8 分的题配 5 个 2 分点，加起来 10 分。但真实高考是"多答不加分"——
学生把 5 个点全答对，最终也只能得 8 分。
所以求和之后必须 min(满分, 合计)，否则会出现"满分 8 分却判出 9 分"。
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import List, Optional

from .models import AiScore, GradingRun, PointResult, RubricPoint


def vote(levels: List[int]) -> tuple:
    """对同一个采分点的多次判定投票，返回 (最终档位, 一致比例)。"""
    if not levels:
        return 0, 0.0
    counter = Counter(levels)
    top = counter.most_common()
    best_n = top[0][1]
    candidates = [lv for lv, n in top if n == best_n]
    if len(candidates) > 1:
        final = int(round(statistics.median(candidates)))
    else:
        final = candidates[0]
    return final, counter[final] / len(levels)


def cap_score(value: float, max_score: Optional[float]) -> float:
    """总分封顶：各采分点加起来超过满分时，只按满分算（多答不加分）。"""
    if not max_score or float(max_score) <= 0:
        return value
    return min(value, float(max_score))


def _run_total(
    run: GradingRun, points: List[RubricPoint], max_score: Optional[float] = None
) -> float:
    level_map = {v.point_seq: v.level for v in run.verdicts}
    raw = sum(p.score * level_map.get(p.seq, 0) / 2 for p in points)
    return cap_score(raw, max_score)


def aggregate(
    student_seq: int,
    points: List[RubricPoint],
    runs: List[GradingRun],
    max_score: Optional[float] = None,
) -> AiScore:
    """把若干次判定汇总成一份答卷的 AI 基准分。

    max_score 是这张卷子的满分（例如 8）。传进来后总分会被封顶：
    候补采分点全答对也不会超过满分。不传（None）则退回"按合计分算"。
    """
    valid_runs = [r for r in runs if r.verdicts]
    if not valid_runs:
        return AiScore(
            student_seq=student_seq,
            total=0.0,
            confidence=0.0,
            score_range=0.0,
            points=[
                PointResult(p.seq, p.text, p.score, 0, 0.0, "（AI 未返回有效判定）", "")
                for p in points
            ],
            comment="AI 阅卷失败，未得到结果",
            low_confidence=True,
        )

    by_point = {p.seq: [] for p in points}
    for run in valid_runs:
        for v in run.verdicts:
            if v.point_seq in by_point:
                by_point[v.point_seq].append(v)

    results: List[PointResult] = []
    agreements: List[float] = []
    total = 0.0

    for p in points:
        verdicts = by_point.get(p.seq, [])
        levels = [v.level for v in verdicts]
        level, agreement = vote(levels)
        if verdicts:
            pick = next((v for v in verdicts if v.level == level), verdicts[0])
            reason = pick.reason
            evidence = pick.evidence
        else:
            reason = "（AI 未返回该点判定）"
            evidence = ""
        total += p.score * level / 2
        agreements.append(agreement)
        results.append(
            PointResult(
                point_seq=p.seq,
                text=p.text,
                score=p.score,
                level=level,
                agreement=agreement,
                reason=reason,
                evidence=evidence,
            )
        )

    totals = [_run_total(r, points, max_score) for r in valid_runs]
    score_range = max(totals) - min(totals) if totals else 0.0
    confidence = sum(agreements) / len(agreements) if agreements else 0.0
    low = confidence < 0.67 or score_range >= 2

    comment = ""
    for r in valid_runs:
        if r.comment:
            comment = r.comment
            break

    return AiScore(
        student_seq=student_seq,
        total=round(cap_score(total, max_score), 2),
        confidence=round(confidence, 3),
        score_range=round(score_range, 2),
        points=results,
        comment=comment,
        low_confidence=low,
    )
