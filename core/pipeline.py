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

    # ★ 2026-10-05 新增：优先从考生档案库抽人（60 人库，按档位比例抽）。
    #   档案带 role_hint（人设）和 error_tendencies（错误类型），
    #   跟 planner 的点名指令【叠加】使用。
    #   ⚠ 抽不到（文件缺失/损坏/库为空）就返回 []，
    #     下面 make_plans 走原有的"按 weights 随机抽"逻辑，程序照常跑。
    picks: List[dict] = []
    plan_profiles: Optional[List[dict]] = None
    try:
        from core import profiles as profile_lib

        picks = profile_lib.pick_profiles(count)
        if picks:
            plan_profiles = profile_lib.to_plan_kwargs(picks)
    except Exception as e:  # noqa: BLE001
        log.error("考生档案库抽样失败：%s（退回随机分组）", e)
        plan_profiles = None

    if picks:
        log.info("从考生档案库抽了 %s 份：%s", len(picks),
                 [p.get("id") for p in picks])
    else:
        log.info("没有可用档案，按权重随机分组")

    plans: List[StudentPlan] = make_plans(
        question, count, weights, blank_rate, style_weights,
        profiles=plan_profiles,
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


def _save_or_reuse_question(question: Question) -> int:
    """题目已存在就更新，不存在才新建。返回 questions.id。

    题库来的题带着 id（导入时就写进库了），走"更新"这条路：
      · 题面改了（教师在首页编辑过）→ 同步过去
      · 采分点改了（教师核对后增删过）→ 整份替换
    ⚠ 存的是 stem（设问），**不是** full_stem()：
      材料在 qbank_files 里，重复写进 stem 会让材料出现两份。
    """
    if not question.id:
        return repo.save_question(question)

    repo.update_question(
        question.id,
        topic=question.topic,
        stem=question.stem,
        max_score=question.max_score,
        points=question.points,
    )
    return int(question.id)


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

    ⚠ 2026-10-04（题库功能）：题目已经存在就别再插一份。
      以前题目都是 AI 现出的，每道都是新的，直接 INSERT 没问题。
      现在题目来自题库、已经躺在 questions 表里了（question.id 不为None），
      再插一次会让同一道题在库里变成好几个 qid —— 抽题记录会指向旧的那些，
      历史记录也会越滚越多、同一道题出现好几遍。
      所以：带id 的直接复用，只把改过的题面/采分点同步过去。
    """
    qid = _save_or_reuse_question(question)
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
