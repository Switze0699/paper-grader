"""全局数据结构。

这里只定义"数据长什么样"，不含任何业务逻辑，也不碰网络和界面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RubricPoint:
    """评分细则里的一个采分点。"""

    seq: int          # 序号，从 1 开始
    text: str         # 采分点表述
    score: float      # 该点分值


@dataclass
class Question:
    subject: str
    topic: str
    stem: str
    max_score: float
    points: List[RubricPoint]
    id: Optional[int] = None

    def rubric_text(self) -> str:
        """渲染成给 AI 看的评分细则文本。"""
        return "\n".join(
            f"{p.seq}. {p.text}（{p.score:g} 分）" for p in self.points
        )


@dataclass
class StudentPlan:
    """生成答卷用的"设计档位"。

    只用来告诉 AI 该写一份什么水平的答案，不参与判分，
    界面上也不会显示，避免干扰教师独立判断。
    """

    seq: int
    ability: str
    # 错误画像：浮于表面型 / 只报结论型 / 术语笼统型 / 半途而止型 / 扎实型
    # 只用来告诉 AI"这份答案主要缺在哪儿"，让半对答案有区分度
    style: str = ""
    # 第一步"抽点"之后分成的四拨（见 planner.split_points）：
    hit: List[int] = field(default_factory=list)
    # ① 完整命中的点（2 分档）：条件 + 因果机制 + 落脚结论三环齐全
    # ② 部分命中的点（1 分档）：只写到条件和一句浅层的话，
    #    采分点要的因果机制没写出来。这是主力手段。
    partial: List[int] = field(default_factory=list)
    # ③ 未命中的点（0 分档，写了但拿不到分）：方向偏了，
    #    或只丢一句结论没有论据。⚠ 不是"答到别的方向上去"——方向红线不许跑题。
    off: List[int] = field(default_factory=list)
    # ④ 没抽到的点：不属于这个学生的"答题范围"，整条不写
    blank: List[int] = field(default_factory=list)
    design_score: float = 0.0
    # 逐点缺陷要求：{采分点序号: 具体做法}，生成答案时逐点下发给 AI。
    # 例如 {2: "答非所问：问自然原因，写成人文因素"}，防止它偷偷写满
    defects: Dict[int, str] = field(default_factory=dict)


@dataclass
class Student:
    seq: int
    ability: str
    answer: str = ""
    design_score: float = 0.0
    # 生成时 AI 的自述："这份答案主要失分在哪儿"。
    # 里面必须用「」标出它打算漏掉/用错的关键词——本地据此机械核对，
    # 防止 AI 嘴上说"只写结论"、笔下照样写成满分。
    flaw: str = ""
    id: Optional[int] = None


@dataclass
class PointVerdict:
    """AI 单次调用里，对某个采分点的判定。"""

    point_seq: int
    level: int        # 0 / 1 / 2
    reason: str
    evidence: str


@dataclass
class GradingRun:
    """AI 对一份答卷的一次完整判定。"""

    run_index: int
    verdicts: List[PointVerdict]
    comment: str = ""
    raw_score: float = 0.0


@dataclass
class PointResult:
    """多次判定投票后，某个采分点的最终结果。"""

    point_seq: int
    text: str
    score: float
    level: int
    agreement: float    # 多次调用中，与最终档位一致的比例
    reason: str
    evidence: str


@dataclass
class AiScore:
    """一份答卷最终得到的 AI 基准分。"""

    student_seq: int
    total: float
    confidence: float          # 0~1，AI 自己判得有多一致
    score_range: float         # 多次调用总分的最大差值
    points: List[PointResult]
    comment: str = ""
    low_confidence: bool = False
