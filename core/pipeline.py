"""把四个步骤串起来：出题 → 生成答卷 → AI 阅卷 → 存档。

界面层和命令行都调用这里，保证两条路走的是同一套逻辑。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from core.models import AiScore, Question, Student, StudentPlan
from core.planner import make_plans
from services.answer_service import generate_answers
from services.grading_service import grade_class
from services.llm import LLMClient
from services.question_service import generate_question
from storage import repository as repo

log = logging.getLogger(__name__)


async def build_question(cfg: dict, api_key: str) -> Question:
    client = LLMClient(cfg, api_key)
    try:
        return await generate_question(client, cfg)
    finally:
        await client.aclose()


async def build_class(
    cfg: dict,
    api_key: str,
    question: Question,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[Student]:
    weights = cfg["classroom"].get("ability_weights") or {}
    style_weights = cfg["classroom"].get("style_weights") or {}
    blank_rate = float(cfg["classroom"].get("blank_rate", 0.0))
    count = int(cfg["classroom"].get("students", 40))

    plans: List[StudentPlan] = make_plans(
        question, count, weights, blank_rate, style_weights
    )
    client = LLMClient(cfg, api_key)
    try:
        students = await generate_answers(client, cfg, question, plans, on_progress)
    finally:
        await client.aclose()

    ok = [s for s in students if s.answer.strip()]
    if len(ok) < len(students):
        log.warning("有 %s 份答卷生成失败，已剔除", len(students) - len(ok))
    for i, s in enumerate(ok, start=1):
        s.seq = i  # 重新编号，保证序号连续
    return ok


async def run_grading(
    cfg: dict,
    api_key: str,
    question: Question,
    students: List[Student],
    on_progress: Optional[Callable[[int, int], None]] = None,
    repeats: Optional[int] = None,
) -> Dict[int, AiScore]:
    client = LLMClient(cfg, api_key)
    try:
        return await grade_class(client, cfg, question, students, on_progress, repeats)
    finally:
        await client.aclose()


def persist(
    question: Question,
    students: List[Student],
    ai_map: Dict[int, AiScore],
    repeats: int,
    timing: Optional[Dict[str, float]] = None,
    api_calls: int = 0,
) -> int:
    """把一次练习完整存档，返回 paper_id。

    timing / api_calls 是 2026-10-04 加的耗时统计（可省略）：
        timing = {"question": 12.3, "answer": 65.0, "grade": 150.2, "total": 228.0}
    """
    qid = repo.save_question(question)
    paper_id = repo.create_paper(qid, repeats)
    repo.save_students(paper_id, students)
    repo.save_ai_scores(paper_id, ai_map)
    t = timing or {}
    repo.save_timing(
        paper_id,
        t_question=t.get("question"),
        t_answer=t.get("answer"),
        t_grade=t.get("grade"),
        t_total=t.get("total"),
        api_calls=api_calls or None,
    )
    return paper_id
