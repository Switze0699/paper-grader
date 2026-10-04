"""对比统计：教师分 vs AI 分。"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from .models import AiScore, Student


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def compare(
    students: List[Student],
    teacher: Dict[int, int],
    ai: Dict[int, AiScore],
) -> dict:
    """输入学生列表、教师给分、AI 给分，输出一整套对比指标。"""
    rows = []
    for s in students:
        t = teacher.get(s.seq)
        a = ai.get(s.seq)
        if t is None or a is None:
            continue
        diff = t - a.total
        rows.append(
            {
                "seq": s.seq,
                "teacher": t,
                "ai": a.total,
                "diff": diff,
                "abs_diff": abs(diff),
                "confidence": a.confidence,
                "low_confidence": a.low_confidence,
            }
        )

    n = len(rows)
    if n == 0:
        return {"n": 0, "rows": rows}

    t_scores = [r["teacher"] for r in rows]
    a_scores = [r["ai"] for r in rows]
    diffs = [r["diff"] for r in rows]

    avg_t = sum(t_scores) / n
    avg_a = sum(a_scores) / n
    bias = avg_t - avg_a
    mae = sum(abs(d) for d in diffs) / n
    exact = sum(1 for d in diffs if abs(d) < 1e-6)
    within1 = sum(1 for d in diffs if abs(d) <= 1)
    loose = sum(1 for d in diffs if d > 1)
    strict = sum(1 for d in diffs if d < -1)

    return {
        "n": n,
        "avg_teacher": round(avg_t, 2),
        "avg_ai": round(avg_a, 2),
        "bias": round(bias, 2),
        "mae": round(mae, 2),
        "exact_rate": round(exact / n, 3),
        "within1_rate": round(within1 / n, 3),
        "loose_count": loose,
        "strict_count": strict,
        "max_abs_diff": round(max(r["abs_diff"] for r in rows), 2),
        "correlation": round(_pearson(t_scores, a_scores), 3)
        if _pearson(t_scores, a_scores) is not None
        else None,
        "low_confidence_count": sum(1 for r in rows if r["low_confidence"]),
        "rows": rows,
    }


def verdict_text(stats: dict) -> tuple:
    """把统计结果翻译成一句话结论，返回 (文字, 颜色)。"""
    bias = stats.get("bias", 0)
    if abs(bias) < 0.3:
        return "你与 AI 考官的尺度基本一致", "#3FA34D"
    if bias > 0:
        return f"你比 AI 考官宽松 {bias:+.2f} 分", "#E8A33D"
    return f"你比 AI 考官严格 {bias:+.2f} 分", "#D62828"
