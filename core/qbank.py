"""题库抽题 + 轮次重置（2026-10-04 新增，纯新增文件，不改任何现有逻辑）。

【为什么单独一个文件】
    抽题是"新功能"，和现有的批改流程一点关系都没有。
    所以这里只依赖 storage.repository，不碰 core.pipeline、app、prompts——
    万一有问题，把这个文件删掉就完全回到现在的状态。

【它负责什么】
    1. 随机抽一道【本轮还没练过】的题
    2. 记下"这个 qid 在第几轮被练过"
    3. 全部题都练过一遍 → 自动开新一轮，从头再来

【轮次是怎么算的】
    qbank_practice 表上有 UNIQUE(qid, round_no)，所以"第 N 轮练过哪些题"
    就是 `SELECT qid FROM qbank_practice WHERE round_no = N`。
    抽题时反着来：取"题库里所有 qid"减去"本轮已练的"，在剩下的里面随机挑一个。

    ⚠ 自动开新一轮的判断：已练数 >= 总题数 时开新一轮。
      用"已练满"而不是"已练完"来判，是为了容忍教师删过题库文件的情况
      （总数变少了，已练的旧记录还在，用取交集的口径才不会卡死）。

【抽到什么长什么样】
    返回一个 Question 对象，和 AI 随机出题时 build_question() 的返回值同构，
    所以后面的流程（生成答卷 → 阅卷 → 盲评 → 报告 → 导出）一行都不用改。
    区别只有两点：
      · 它已经带着材料（Question.material）和采分点，不需要教师再确认细则
      · q.id 不为None，是题库里的真实题号，存档能溯源
"""

from __future__ import annotations

import logging
import random
from typing import List, NamedTuple, Optional

from core.models import Question
from storage import repository as repo

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------

class Pick(NamedTuple):
    """抽题结果。

    question 为 None 表示题库里一题都没有（该去导入 txt 了），
    这时看reason 才知道该提示用户什么。
    """

    question: Optional[Question]
    round_no: int = 0
    remaining: int = 0        # 抽完之后，本轮还剩几道没练
    total: int = 0            # 题库一共几道
    reason: str = ""           # 人话原因，给界面直接显示

    @property
    def ok(self) -> bool:
        return self.question is not None


# ---------------------------------------------------------------------------
# 轮次
# ---------------------------------------------------------------------------

def current_round(advance: bool = True) -> int:
    """当前是第几轮。

    规则：
      · 题库里一题都没有 → 返回 0（还没开始）
      · 最近一轮还没练完 → 就是那一轮
      · 最近一轮已经练满 → 开新一轮（并写进 qbank_rounds）

    ⚠ advance=False 只**查不动**：练满时仍然返回"练满的那一轮"，
      用来让界面显示"这一轮已练完，下一题会开新一轮"。
      如果这里也开新一轮，那个"已练满"的状态就没地方体现了 ——
      （实测踩过：抽完最后一道，进度立刻显示"第 2 轮、已练 0 道"，
      教师永远看不到自己这一轮练完了。）
    """
    total = repo.bank_questions_count()
    if total <= 0:
        return 0

    last = _last_round()
    if last <= 0:
        return _open_round()

    if _practiced_count(last) < total:
        return last

    if not advance:
        return last       #练满了，但先别开新一轮（只给人看）

    nxt = last + 1
    if _open_round_if_absent(nxt):
        log.info("题库：第 %s 轮已全部练完，自动开始第 %s 轮", last, nxt)
    return nxt


def _last_round() -> int:
    """最近开过的那一轮（没有就返回 0）。"""
    from storage.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(round_no) AS r FROM qbank_rounds"
        ).fetchone()
        return int(row["r"] or 0)
    finally:
        conn.close()


def _open_round() -> int:
    """开第 1 轮。"""
    _open_round_if_absent(1)
    return 1


def _open_round_if_absent(round_no: int) -> bool:
    """开新一轮。已经开过就什么都不做，返回 False。"""
    from datetime import datetime

    from storage.db import get_conn

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO qbank_rounds (round_no, started_at)"
            " VALUES (?,?)",
            (int(round_no), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _practiced_count(round_no: int) -> int:
    """这一轮已经练过几道（只算现在还在题库里的）。"""
    from storage.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM qbank_practice p"
            " JOIN qbank_file_questions fq ON fq.qid = p.qid"
            " WHERE p.round_no = ?",
            (int(round_no),),
        ).fetchone()
        return int(row["c"] or 0)
    finally:
        conn.close()


def _practiced_qids(round_no: int) -> set:
    """这一轮已经抽过哪些 qid。"""
    from storage.db import get_conn

    conn = get_conn()
    try:
        return {
            int(r["qid"]) for r in conn.execute(
                "SELECT qid FROM qbank_practice WHERE round_no=?",
                (int(round_no),),
            ).fetchall() if r["qid"]
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 抽题
# ---------------------------------------------------------------------------

def pick(seed: Optional[int] = None) -> Pick:
    """随机抽一道本轮没练过的题。

    seed 只在测试里用（固定随机数 → 结果可复现），平时不传。
    """
    pool = repo.load_bank_questions()
    if not pool:
        return Pick(
            question=None, round_no=0, remaining=0, total=0,
            reason="题库还是空的。请把试题的 .txt 放进「题库」文件夹，"
                   "再运行一次导入。",
        )

    rnd = current_round()
    done = _practiced_qids(rnd)
    left = [q for q in pool if (q.id is None or q.id not in done)]

    # 理论上不会走到这里（current_round 会在练满时开新一轮），
    # 但万一并发动手抽了两下、记录还没落库，这里兜住，别让教师卡住。
    if not left:
        nxt = rnd + 1
        _open_round_if_absent(nxt)
        left = pool
        rnd = nxt
        log.warning("题库：本轮已无剩余，临时再开一轮（第 %s 轮）", nxt)

    rng = random.Random(seed) if seed is not None else random
    q = rng.choice(left)
    _mark_practiced(q.id, rnd)

    log.info("题库：第 %s 轮抽到 qid=%s（还剩 %s 道）", rnd, q.id, len(left) - 1)
    return Pick(question=q, round_no=rnd, remaining=len(left) - 1,
                total=len(pool))


def _mark_practiced(qid: Optional[int], round_no: int) -> None:
    """记一笔"这个 qid 在这一轮被抽过"。

    ⚠ 是在【抽到的时候】就记，不是等批改完才记。
       理由：教师可能抽完就关掉程序。记在抽的那一刻才保证"一轮里不重复"。
       UNIQUE(qid, round_no) 保证同轮不会记重。
    """
    if qid is None:
        return
    from datetime import datetime

    from storage.db import get_conn

    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO qbank_practice (qid, round_no, practiced_at)"
            " VALUES (?,?,?)",
            (int(qid), int(round_no),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def link_paper(qid: Optional[int], round_no: int, paper_id: int) -> None:
    """批改存档之后，把这次练习和存档号对起来（报告里溯源用）。"""
    if qid is None or not paper_id:
        return
    from storage.db import get_conn

    conn = get_conn()
    try:
        conn.execute(
            "UPDATE qbank_practice SET paper_id=? WHERE qid=? AND round_no=?",
            (int(paper_id), int(qid), int(round_no)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 给界面看的进度
# ---------------------------------------------------------------------------

def progress() -> dict:
    """当前进度，给首页显示用。

    返回 {"total","round_no","done","remaining","finished"}：
      done      这一轮已经练过几道
      remaining  这一轮还剩几道
      finished  是不是这一轮已经练满了（练满时界面提示"下一题会开新一轮"）

    ⚠ 用current_round(advance=False)：这里**只看不改**。
      练满时它返回"练满的那一轮"，于是界面能显示"本轮 3/3 已练完"，
      同时不会因为看一眼进度就莫名其妙开新一轮。
    """
    total = repo.bank_questions_count()
    if total <= 0:
        return {"total": 0, "round_no": 0, "done": 0, "remaining": 0,
                "finished": False}

    rnd = current_round(advance=False)
    done = _practiced_count(rnd)
    return {
        "total": total,
        "round_no": rnd,
        "done": done,
        "remaining": max(0, total - done),
        "finished": done >= total,
    }


def reset_all() -> int:
    """清空所有练习记录，回到第 1 轮。

    教师想从头再练一遍时用。返回删掉的记录条数。
    """
    from storage.db import get_conn

    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM qbank_practice").fetchone()["c"]
        conn.execute("DELETE FROM qbank_practice")
        conn.execute("DELETE FROM qbank_rounds")
        conn.commit()
    finally:
        conn.close()
    _open_round()
    log.info("题库：练习记录已清空（共%s 条），回到第 1 轮", n)
    return int(n or 0)
