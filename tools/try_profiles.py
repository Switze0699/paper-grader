# -*- coding: utf-8 -*-
"""试演探针：验证【档案库 → 生成端】正式接入方式（叠加）的效果。

⚠ 只跑少量（用户 2026-10-05 要求先跑 2 份，别烧额度）。
⚠ 走的是正式链路：core.profiles 抽档案 → planner.make_plans 建计划 →
   answer_service 生成 → grading_service 阅卷。不再手工 hack student_role。

用法：
    .venv/Scripts/python.exe tools/try_profiles.py --ids 22 57
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import profiles as prof_mod  # noqa: E402
from core.config import get_api_key, load_config  # noqa: E402
from core.planner import WRONG_KINDS, make_plans  # noqa: E402
from services.answer_service import build_student_block, generate_answers  # noqa: E402
from services.grading_service import grade_class  # noqa: E402
from services.llm import LLMClient  # noqa: E402
from storage import repository as repo  # noqa: E402


def pick_by_ids(ids) -> list:
    """按 id 精确取档案（测试用，保证能复现同一批人）。"""
    allp = prof_mod.load_profiles()
    by_id = {p["id"]: p for p in allp}
    want = ids or [22, 57]
    missing = [i for i in want if i not in by_id]
    if missing:
        sys.exit(f"档案库里没有这些 id: {missing}")
    return [by_id[i] for i in want]


def show_quota() -> None:
    """演示默认抽样：抽 8 份时的档位分布。"""
    allp = prof_mod.load_profiles()
    q = prof_mod.quota_for(8, allp)
    print(f"总库 {len(allp)} 份；抽 8 份的配额 → {q}（合计 {sum(q.values())}）")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", type=int, nargs="*", default=None)
    ap.add_argument("--question", type=int, default=None, help="qid，默认取第一道")
    args = ap.parse_args()

    cfg = load_config()
    api_key = get_api_key(cfg)
    if not api_key:
        sys.exit("没读到 ZHIPU_API_KEY，检查 .env")

    show_quota()

    qs = repo.load_bank_questions()
    if not qs:
        sys.exit("题库读不到题")
    qid = args.question
    question = next((q for q in qs if q.id == qid), qs[0]) if qid else qs[0]

    print("=" * 74)
    print(f"[qid {question.id}] {question.full_stem()[:120]}…")
    print(f"满分 {question.max_score:g}   采分点 {len(question.points)} 个")
    print("=" * 74)

    picks = pick_by_ids(args.ids)
    plans = make_plans(
        question, len(picks), weights={}, profiles=prof_mod.to_plan_kwargs(picks)
    )

    # 先看提示词里错误指令和人物设定有没有对上（不花额度）
    for prof, plan in zip(picks, plans):
        declared = [k for k in (prof.get("error_tendencies") or []) if k in WRONG_KINDS]
        print()
        print(f"档案 #{prof['id']} {prof['name']} [{prof['level']}]")
        print(f"  档案声明错误 → {declared}")
        print(f"  plan.wrong_kinds → {plan.wrong_kinds}"
              f"   {'一致' if plan.wrong_kinds == declared else '不一致'}")
        print(f"  role_hint 已写入 plan → {'是' if plan.role_hint else '否'}")
        block = build_student_block(question, plan)
        print(f"  提示词含【点名改错】→ {'是' if '点名改错' in block else '否'}"
              f"  含【人物设定】→ {'是' if '【人物设定】' in block else '否'}")

    # ---------------- 真正生成 + 阅卷（这段花额度） ----------------
    client = LLMClient(cfg, api_key=api_key)
    students = await generate_answers(client, cfg, question, plans)

    print()
    print("=" * 74)
    print("生成结果")
    print("=" * 74)
    for prof, stu in zip(picks, students):
        print()
        print(f"【档案 #{prof['id']} {prof['name']} · {prof['level']}】"
              f"  {len(stu.answer)} 字")
        print(stu.answer)

    print()
    print("=" * 74)
    print("阅卷结果（repeats=1，只为快速看效果）")
    print("=" * 74)
    scores = await grade_class(client, cfg, question, students, repeats=1)
    for prof, stu in zip(picks, students):
        sc = scores.get(stu.seq)
        if sc:
            rate = sc.total / question.max_score * 100
            head = f"满 {question.max_score:g}  得 {sc.total:g}  → {rate:.0f}%"
        else:
            head = "满 %g  得 ?" % question.max_score
        print()
        print(f"【#{prof['id']} {prof['name']} · {prof['level']}】 {head}")
        print(f"  档案声明错误：{prof['error_tendencies']}")
        if sc:
            for v in sc.points:
                ct = {0: "零分", 1: "一半", 2: "满分"}.get(v.level, str(v.level))
                print(f"    [{ct}] {v.score:g}分  {v.text[:34]}")
                if v.reason:
                    print(f"         理由：{v.reason}")
                if v.evidence:
                    print(f"         证据：{v.evidence}")
            if sc.comment:
                print(f"  点评：{sc.comment}")


if __name__ == "__main__":
    asyncio.run(main())
