"""第三步：AI 阅卷。

核心稳定性设计：
· 温度固定为 0，尽量消除随机
· 不让 AI 直接给总分，只让它对每个采分点判 0/1/2
· 同一份答卷独立判定 N 次，逐点投票，再按分值求和
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from core.aggregator import aggregate
from core.models import AiScore, GradingRun, PointVerdict, Question, Student
from services.prompts import fill, load_prompt

log = logging.getLogger(__name__)


def _clean_level(value) -> int:
    try:
        lv = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return 0 if lv < 0 else (2 if lv > 2 else lv)


def _build_prompts(cfg: dict, question: Question, answer: str) -> tuple:
    system = fill(
        load_prompt("grading_system.txt"), subject=question.subject
    )
    user = fill(
        load_prompt("grading_user.txt"),
        # ⚠ 2026-10-04 必须用 full_stem()，不能用 stem：
        #   阅卷的判据是"这一点【条件／现象】有没有点出来"，
        #   而条件／现象就出自材料。题库来的题材料不在 stem 里，
        #   只发 stem 的话阅卷官看不到材料 → 判分必然出错。
        #   AI 随机出题的老题 material 为空，full_stem() 原样返回 stem，行为不变。
        question=question.full_stem(),
        max_score=f"{question.max_score:g}",
        rubric=question.rubric_text(),
        answer=answer or "（该生未作答）",
    )
    return system, user


async def _one_call(client, cfg: dict, question: Question, student: Student,
                    run_index: int) -> GradingRun:
    system, user = _build_prompts(cfg, question, student.answer)
    try:
        data = await client.chat_json(
            system=system,
            user=user,
            temperature=float(cfg["grading"].get("temperature", 0.0)),
        )
    except Exception as e:  # noqa: BLE001
        log.error("第 %s 份答卷第 %s 次判定失败：%s", student.seq, run_index, e)
        return GradingRun(run_index=run_index, verdicts=[])

    verdicts: List[PointVerdict] = []
    for item in data.get("points") or []:
        if not isinstance(item, dict):
            continue
        try:
            seq = int(item.get("index", len(verdicts) + 1))
        except (TypeError, ValueError):
            seq = len(verdicts) + 1
        verdicts.append(
            PointVerdict(
                point_seq=seq,
                level=_clean_level(item.get("level", 0)),
                reason=str(item.get("reason", "")).strip(),
                evidence=str(item.get("evidence", "")).strip(),
            )
        )
    return GradingRun(
        run_index=run_index,
        verdicts=verdicts,
        comment=str(data.get("comment", "")).strip(),
    )


async def grade_class(
    client,
    cfg: dict,
    question: Question,
    students: List[Student],
    on_progress: Optional[Callable[[int, int], None]] = None,
    repeats: Optional[int] = None,
) -> Dict[int, AiScore]:
    """给一整批答卷判分，返回 {学生序号: AiScore}。"""
    g = cfg["grading"]
    n = int(repeats or g.get("repeats", 3))
    concurrency = max(1, int(g.get("concurrency", 8)))

    sem = asyncio.Semaphore(concurrency)
    total_calls = len(students) * n
    completed = 0
    lock = asyncio.Lock()

    async def call(student: Student, idx: int):
        nonlocal completed
        async with sem:
            result = await _one_call(client, cfg, question, student, idx)
        async with lock:
            completed += 1
            if on_progress:
                on_progress(completed, total_calls)
        return student.seq, result

    pairs = [(s, i) for s in students for i in range(n)]
    raw = await asyncio.gather(*[call(s, i) for s, i in pairs])

    grouped: Dict[int, List[GradingRun]] = {s.seq: [] for s in students}
    for seq, run in raw:
        grouped.setdefault(seq, []).append(run)

    return {
        # 传满分进去：候补采分点全答对也不会超过满分（多答不加分）
        seq: aggregate(seq, question.points, runs, question.max_score)
        for seq, runs in grouped.items()
    }
