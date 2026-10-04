"""计时与调用计数：统计一次批改各步骤花了多久、发了多少次 API 请求。

为什么要这个：
    用户问"批改一份要多久"，光看总时间没意义 —— 得知道是出题慢、
    生成答卷慢，还是 AI 阅卷慢。所以三步分开计时。
    另外记一下 API 调用次数，方便评估额度消耗（阅卷是 repeats 次/份，
    8 份 × 3 次 = 24 次，一次批改下来不算少）。

用法（界面里已经在用了，见 app/screens/setup.py）：
    from core.timing import Stopwatch
    sw = Stopwatch()
    q = await build_question(...)
    sw.mark("question")
    students = await build_class(...)
    sw.mark("answer")
    ai = await run_grading(...)
    sw.mark("grade")
    print(sw.summary())      # 人类可读
    persist(..., timing=sw)  # 存档
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

# 三步的显示名（报告页和 Excel 都用这个顺序）
STEP_LABELS = {
    "question": "出题",
    "answer": "生成答卷",
    "grade": "AI 阅卷",
    "total": "合计",
}
STEP_ORDER = ["question", "answer", "grade"]

# ⚠ 键名约定：**一律用不带 t_ 前缀的这套**（question/answer/grade/total）。
#   数据库 papers 表的列名带 t_ 前缀（t_question/t_answer/t_grade/t_total），
#   但那只是列名 —— 读出来之后必须转成上面这套再往下传。
#   （两边混用过一次，导致耗时卡片默默返回 None，界面上什么都不显示。）


def fmt_duration(seconds: Optional[float]) -> str:
    """把秒数说成人话：0.8 秒 / 12 秒 / 1 分 05 秒 / 1 小时 02 分。

    用户看报告要一眼能懂，所以单位按大小自动切换。
    """
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        s = 0.0
    if s < 10:
        return f"{s:.1f} 秒"
    if s < 60:
        return f"{s:.0f} 秒"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} 分 {sec:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m:02d} 分"


class Stopwatch:
    """按步骤累计耗时。

    用法：new 一个 → 每做完一步 mark(名字) → 读 .steps / .total。
    嵌套也能用（外层算总耗时、内层算某一步），互不干扰。
    """

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.steps: Dict[str, float] = {}
        # 记录每一步开始时的绝对时间，用来算"这一步什么时候开始的"
        self._marks: List[str] = []

    def mark(self, name: str) -> float:
        """记下这一步的耗时（从上一个 mark 或构造时算起），返回秒数。"""
        now = time.perf_counter()
        # 同名重复 mark（重试场景）就累加，而不是覆盖
        self.steps[name] = self.steps.get(name, 0.0) + (now - self._last)
        self._last = now
        self._marks.append(name)
        return self.steps[name]

    @property
    def total(self) -> float:
        """从 new 到现在的总耗时。"""
        return time.perf_counter() - self._t0

    def snapshot_total(self) -> float:
        """取一次当前的总耗时（存档时用，之后继续计时不受影响）。"""
        return time.perf_counter() - self._t0

    def as_dict(self) -> Dict[str, float]:
        """给存档用：{'question': 12.3, 'answer': 65.0, 'total': 220.5}"""
        d = dict(self.steps)
        d["total"] = self.snapshot_total()
        return d

    def summary(self) -> str:
        """人类可读的一行摘要，给日志和黑窗口用。"""
        parts = [f"{STEP_LABELS.get(k, k)} {fmt_duration(v)}"
                 for k, v in self.steps.items()]
        total = self.snapshot_total()
        parts.append(f"合计 {fmt_duration(total)}")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# API 调用计数
# ---------------------------------------------------------------------------
# 所有 API 请求都走 services/llm.py 的 LLMClient，所以在那里加一个
# 模块级计数器就够了 —— 不改任何调用点的签名。

_call_count = 0


def get_api_calls() -> int:
    """本进程累计发出的 API 请求数。"""
    return _call_count


def reset_api_calls() -> None:
    """一次批改开始前调用，把计数清零。"""
    global _call_count
    _call_count = 0


def note_api_call(n: int = 1) -> None:
    """每发一次请求调一下。"""
    global _call_count
    _call_count += max(0, int(n))


def timing_summary_line(info: Dict[str, float], calls: int,
                        n_students: int = 0) -> str:
    """给界面/日志用的一行摘要。"""
    parts = []
    for k in STEP_ORDER:
        if k in info:
            parts.append(f"{STEP_LABELS[k]} {fmt_duration(info[k])}")
    parts.append(f"{STEP_LABELS['total']} {fmt_duration(info.get('total'))}")
    line = " · ".join(parts)
    if calls:
        line += f" · API {calls} 次"
        if n_students:
            line += f"（每份约 {calls / max(1, n_students):.1f} 次）"
    return line
