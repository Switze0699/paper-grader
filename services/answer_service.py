"""第二步：批量生成学生答卷 —— 【考生视角，看不到评分细则】。

═══════════════════════════════════════════════════════════════
2026-09-29 用户要求 · 核心改动（改这里之前先读懂）
═══════════════════════════════════════════════════════════════
【旧做法的问题】
以前生成答卷时把【题目 + 完整评分细则 + 逐点"该写残/该写满"的指令】
一起发给 AI，于是它总是贴着标准答案写，答案太完美、不像真人。
而且它知道每一条该写什么，等于开卷考试——不符合真实考场逻辑。

【新做法】
1. 生成答卷时【只发题目材料 + 设问 + 角色提示词】，
   **严禁把评分细则发给它**。AI 像真正的考生一样，只凭自己的知识储备
   和理解作答。它不知道采分点是什么，自然会遗漏、偏差、不精准。
2. 学生之间的水平差异【靠角色提示词控制】，不再靠"看答案决定哪条写残"：
   优秀 / 良好 / 中等 / 薄弱 / 很差（见 planner.ROLE_PROMPTS），
   再叠加一份"答题习惯"（planner.STYLE_TRAITS，如浮于表面型、似是而非型），
   让同一水平的答案也有个体差异。
3. 阅卷照旧给【完整评分细则】——AI 阅卷官逐点判学生写了什么、漏了什么、
   哪里不精确。总分仍按 min(题面满分, 各点之和) 封顶。
4. 本地闸门只剩两道：跑方向（违禁词）与写得过短（残句）。
   "写得跟标准答案太像"这件事已经不可能发生了——它根本没见过标准答案。

⚠ 一次请求只写一位学生：每位学生的角色提示词不同，混在一批里写会互相串味。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional

from core.models import Question, Student, StudentPlan
from core.planner import expected_points, length_bounds, length_hint, student_role
from core.quality import (
    answer_length_problem,
    class_length_problem,
    clean_answer,
    direction_violations,
)
from services.prompts import fill, load_prompt

log = logging.getLogger(__name__)


def _length_for(question: Question, plan: StudentPlan) -> str:
    """这份答卷的篇幅参考（按档位 + 题面需要几个点来估算）。"""
    point_score = question.points[0].score if question.points else 2.0
    n = expected_points(question.max_score, point_score)
    return length_hint(plan.ability, n)


def build_student_block(question: Question, plan: StudentPlan) -> str:
    """把"这位学生是谁"翻译成提示词块（不含任何评分细则的内容）。"""
    point_score = question.points[0].score if question.points else 2.0
    n_expect = expected_points(question.max_score, point_score)
    lo_l, hi_l, _lo_c, _hi_c = length_bounds(plan.ability, n_expect)
    # ⚠ wrong_kinds 必须传 plan 里那一份，不能让 student_role 现抽——
    #   现抽的话，生成用的和下面质量闸门用的会是两个不同名单。
    # n_lines 传"预计写几条"，用来给错误分配具体条号（从第 2 条起铺开）——
    #   实测：只说"有一处会答错"AI 不听，必须点名"第3 条写成张冠李戴"。
    # ★ 2026-10-05 第二轮：role_hint（来自考生档案库）当【人物设定】传进去，
    #   错误指令仍由 planner 点名条号生成——两者【叠加】，谁也不替谁。
    #   实测只喂 role_hint 时 AI 五条全写对，一条错误都注入不进去。
    role = student_role(plan.ability, plan.style, plan.wrong_kinds,
                        (lo_l + hi_l) // 2, role_hint=plan.role_hint)
    return (
        f"学生编号：{plan.seq}\n"
        f"水平：{plan.ability}\n"
        f"角色设定：{role}\n"
        f"篇幅参考：{_length_for(question, plan)}"
    )


async def _gen_one(
    client,
    cfg: dict,
    question: Question,
    plan: StudentPlan,
) -> Student:
    """生成一位学生的答卷（只给题目 + 角色，不给评分细则）。"""
    system = fill(load_prompt("answer_system.txt"), subject=question.subject)
    user = fill(
        load_prompt("answer_user.txt"),
        # ⚠ 2026-10-04 必须用 full_stem()，不能用 stem：
        #   题库来的题，"材料"和"设问"是分开两段（材料存在 qbank_files 里），
        #   只发 stem 的话学生**看不到材料**，答卷必然凭空编。
        #   AI 随机出题的老题 material 为空，full_stem() 原样返回 stem，行为不变。
        question=question.full_stem(),
        max_score=f"{question.max_score:g}",
        student_block=build_student_block(question, plan),
    )
    data = await client.chat_json(
        system=system,
        user=user,
        temperature=float(cfg["classroom"].get("answer_temperature", 0.9)),
    )
    text = ""
    if isinstance(data, dict):
        text = str(data.get("answer", "") or "").strip()
    elif isinstance(data, str):
        text = data.strip()
    return Student(
        seq=plan.seq,
        ability=plan.ability,
        answer=clean_answer(text),
        design_score=plan.design_score,
    )


async def generate_answers(
    client,
    cfg: dict,
    question: Question,
    plans: List[StudentPlan],
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[Student]:
    """并发生成答卷（每人一次请求），返回的学生顺序与 plans 一致。"""
    concurrency = max(1, int(cfg["classroom"].get("answer_concurrency", 5)))
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def worker(plan: StudentPlan) -> Student:
        nonlocal done
        async with sem:
            try:
                return await _gen_one(client, cfg, question, plan)
            except Exception as e:  # noqa: BLE001
                log.error("第 %s 份答卷生成失败：%s", plan.seq, e)
                return Student(seq=plan.seq, ability=plan.ability, answer="",
                               design_score=plan.design_score)
            finally:
                done += 1
                if on_progress:
                    on_progress(min(done, len(plans)), len(plans))

    results = list(await asyncio.gather(*(worker(p) for p in plans)))
    results.sort(key=lambda s: s.seq)
    results = await quality_gate(client, cfg, question, plans, results)
    return results


async def _fix_answer(
    client,
    cfg: dict,
    question: Question,
    plan: StudentPlan,
    answer: str,
    problems: List[str],
) -> str:
    """就地改写：只修"违反作答规范"的地方（跑方向、口语、太短），其余不动。

    ⚠ 这里同样【不给评分细则】——只告诉它题目和学生身份，
      否则等于让它照着标准答案补内容，又回到"答案太完美"的老路。
    """
    system = fill(load_prompt("answer_system.txt"), subject=question.subject)
    user = fill(
        load_prompt("answer_fix.txt"),
        question=question.full_stem(),   # ⚠ 同上：必须带材料
        answer=answer,
        role=student_role(plan.ability, plan.style),
        ability=plan.ability,
        problems="；".join(problems),
        length=_length_for(question, plan),
    )
    data = await client.chat_json(
        system=system,
        user=user,
        temperature=float(cfg["classroom"].get("answer_temperature", 0.9)),
    )
    if isinstance(data, dict):
        return str(data.get("answer", "") or "").strip()
    if isinstance(data, str):
        return data.strip()
    return ""


async def quality_gate(
    client,
    cfg: dict,
    question: Question,
    plans: List[StudentPlan],
    results: List[Student],
) -> List[Student]:
    """生成后的本地闸门：只看【有没有跑方向】和【篇幅对不对】。

    生成时 AI 已经看不到评分细则，所以旧版那套"跟评分标准比对、
    核对自述要漏哪段因果"的判据全都用不着了（也不可能触发）。
    保留这一道闸门是为了拦住三类硬伤：
      · 答案里冒出跟题目问法不符的方向（问自然写经济、问有利写制约）
      · AI 把答案压成残句（"3. 黄土。"），一眼就能看出不是真人写的
      · 【2026-09-29 用户要求】同一班里有人的篇幅跟其他人明显不成比例
        ——"好学生写一堆、差生一点不写"这种反差一眼就是假的，
        拿它练批改会把教师的判断带偏。判据是全班字数的中位数（见
        class_length_problem），不是各档的绝对字数。
    最多改写 1~2 轮，改不动就保留原稿（不死循环、不无限花钱）。
    """
    classroom = cfg.get("classroom", {})
    if not classroom.get("quality_gate", True) or client is None:
        return results

    max_rounds = max(0, int(classroom.get("regenerate_rounds", 1)))
    forbidden = None
    if classroom.get("direction_guard", True):
        forbidden = classroom.get("forbidden_words") or None
    plan_by_seq = {p.seq: p for p in plans}

    def check(answer: str, plan: StudentPlan) -> List[str]:
        # ⚠ allowed = 这一份【故意】要犯的方向错误条数（2026-10-05）。
        #   生成端现在按planner.WRONG_QUOTA 注入「答非所问」类错误来压得分率，
        #   闸门要放行这些错误——否则会把它们"修"掉，白烧额度还压不下分。
        #   超出配额才算真跑方向，那才是要拦的硬伤。
        allowed = len(plan.wrong_kinds or [])
        problems = direction_violations(question, answer, forbidden, allowed)
        short = answer_length_problem(question, plan, answer)
        if short:
            problems.append(short)
        return problems

    for round_no in range(1, max_rounds + 1):
        # ① 全班篇幅均衡：先按中位数整体看一眼（只跟同班同学比，
        #    不跟各档的绝对字数比——那样会把差生一律判成不合规）
        cls_problems: dict = {}
        if classroom.get("length_balance", True):
            cls_problems = class_length_problem(
                {st.seq: st.answer for st in results},
                float(classroom.get("length_min_ratio", 0.6)),
                float(classroom.get("length_max_ratio", 1.6)),
            )
            if cls_problems:
                log.info("质量闸门：全班篇幅不整齐，%s 份需要调整（%s）",
                         len(cls_problems),
                         "、".join(f"第{s}份" for s in sorted(cls_problems)))

        bad: List[tuple] = []
        for st in results:
            plan = plan_by_seq.get(st.seq)
            if plan is None or not st.answer.strip():
                continue
            problems = check(st.answer, plan)
            extra = cls_problems.get(st.seq)
            if extra:
                problems.append(extra)
            if problems:
                bad.append((st, plan, problems))

        if not bad:
            if round_no > 1:
                log.info("质量闸门：第 %s 轮后全部合格", round_no)
            return results

        log.info(
            "质量闸门：第 %s 轮检出 %s 份不合格（跑方向／写成残句），正在就地改写",
            round_no,
            len(bad),
        )
        for st, plan, problems in bad:
            log.info("  第 %s 份问题：%s", st.seq, "；".join(problems))
            try:
                fixed = await _fix_answer(
                    client, cfg, question, plan, st.answer, problems
                )
            except Exception as e:  # noqa: BLE001
                log.warning("改写第 %s 份失败：%s", st.seq, e)
                continue
            if not fixed:
                log.warning("第 %s 份改写返回空，保留原稿", st.seq)
                continue
            fixed = clean_answer(fixed)
            # 改写后问题变少、或者篇幅没缩水，就采用；否则保留原稿
            if len(check(fixed, plan)) < len(problems) and len(fixed) >= len(st.answer) * 0.6:
                st.answer = fixed
            else:
                log.info("第 %s 份改写后没有改善，保留原稿", st.seq)

    return results
