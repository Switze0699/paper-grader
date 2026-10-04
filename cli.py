"""命令行模式：不用界面也能跑完整流程。

主要用来排查问题（比如"是不是密钥没配对"），平时不用管它。
用法示例：
    .venv\\Scripts\\python.exe cli.py --check          只测密钥和接口通不通
    .venv\\Scripts\\python.exe cli.py --students 4 --repeats 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from core.config import get_api_key, load_config, setup_logging
from core.pipeline import build_class, build_question, persist, run_grading
from core.report import compare, verdict_text
from core.timing import (Stopwatch, get_api_calls, reset_api_calls,
                         timing_summary_line)
from services.llm import LLMClient
from storage import repository as repo

log = logging.getLogger(__name__)


async def check_async() -> None:
    """只做一件事：确认密钥、地址、模型三者对得上。"""
    cfg = load_config()
    key = get_api_key(cfg)
    ai = cfg.get("ai", {})
    print(f"接口地址：{ai.get('base_url')}")
    print(f"模型：{ai.get('model')}")
    print(f"思维链：{ai.get('thinking') or '不传该参数'}　"
          f"贪心解码：{ai.get('greedy_when_zero')}")
    if not key:
        print(f"\n没读到密钥。请在 .env 里填写 {ai.get('api_key_env')}=你的智谱 API Key")
        return
    print(f"密钥：已读取（{ai.get('api_key_env')}，长度 {len(key)}）")

    client = LLMClient(cfg, key)
    try:
        print("\n正在发送测试请求……")
        print(await client.ping())
        print("\n一切正常，可以双击「start_desktop.bat」使用了。")
    finally:
        await client.aclose()


async def main_async(args) -> None:
    cfg = load_config()
    key = get_api_key(cfg)
    if not key:
        print("没有读到密钥，请先在 .env 里配置 ZHIPU_API_KEY=你的智谱 API Key。")
        return

    print("\n[1/3] AI 正在出题……")
    sw = Stopwatch()
    reset_api_calls()
    q = await build_question(cfg, key)
    sw.mark("question")
    print(f"题目：{q.full_stem()}")   # ⚠ 带材料
    print(f"主题：{q.topic}　满分：{q.max_score:g}")
    print("评分细则：")
    for p in q.points:
        print(f"  {p.seq}. {p.text}（{p.score:g} 分）")

    cfg["classroom"]["students"] = args.students

    print(f"\n[2/3] 正在生成 {args.students} 份学生答卷……")

    def p1(done, total):
        print(f"\r    生成中 {done}/{total}", end="", flush=True)

    students = await build_class(cfg, key, q, p1)
    sw.mark("answer")
    print(f"\n    完成，有效答卷 {len(students)} 份")

    print(f"\n[3/3] AI 阅卷（每份判定 {args.repeats} 次）……")

    def p2(done, total):
        print(f"\r    阅卷中 {done}/{total}", end="", flush=True)

    ai = await run_grading(cfg, key, q, students, p2, repeats=args.repeats)
    sw.mark("grade")
    print()

    # 命令行模式没有真人教师，这里用"设计档位分"充当教师分，只为演示报告
    teacher = {s.seq: int(round(s.design_score)) for s in students}
    _info = sw.as_dict()
    _calls = get_api_calls()
    paper_id = persist(q, students, ai, args.repeats,
                       timing=_info, api_calls=_calls)
    for seq, score in teacher.items():
        repo.save_teacher_grade(paper_id, seq, score)
    repo.set_paper_status(paper_id, "done")

    print("\n===== 本次批改耗时 =====")
    print(f"  {timing_summary_line(_info, _calls, len(students))}")

    print("\n===== AI 阅卷明细（第 1 份示例）=====")
    first = ai.get(students[0].seq)
    if first:
        for pr in first.points:
            print(f"  第{pr.point_seq}点 档位={pr.level} 得分={pr.score * pr.level / 2:g} "
                  f"一致率={pr.agreement:.2f}")
            print(f"     理由：{pr.reason}")
            print(f"     证据：{pr.evidence or '（无）'}")
        print(f"  AI 总分：{first.total}　置信度：{first.confidence:.2f}")

    stats = compare(students, teacher, ai)
    verdict, _ = verdict_text(stats)
    print("\n===== 对比报告（教师分用设计分模拟）=====")
    print(f"  份数：{stats['n']}")
    print(f"  教师平均：{stats['avg_teacher']}　AI 平均：{stats['avg_ai']}")
    print(f"  偏差：{stats['bias']:+.2f}　平均绝对误差：{stats['mae']}")
    print(f"  完全一致率：{stats['exact_rate'] * 100:.0f}%　"
          f"相差≤1分：{stats['within1_rate'] * 100:.0f}%")
    print(f"  结论：{verdict}")
    print(f"\n已存档，记录编号：{paper_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--students", type=int, default=4, help="生成几份答卷")
    parser.add_argument("--repeats", type=int, default=2, help="每份判定几次")
    parser.add_argument("--check", action="store_true",
                        help="只检查密钥和接口是否连通")
    args = parser.parse_args()
    setup_logging()
    if args.check:
        asyncio.run(check_async())
    else:
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
