"""数据读写：存档、读取历史、取回某次练习的全部数据。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from core.models import AiScore, Question, RubricPoint, Student
from storage.db import get_conn

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_question(q: Question) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO questions (subject, topic, stem, max_score, created_at)"
            " VALUES (?,?,?,?,?)",
            (q.subject, q.topic, q.stem, q.max_score, _now()),
        )
        qid = int(cur.lastrowid)
        for p in q.points:
            conn.execute(
                "INSERT INTO rubric_points (question_id, seq, text, score)"
                " VALUES (?,?,?,?)",
                (qid, p.seq, p.text, p.score),
            )
        conn.commit()
        q.id = qid
        return qid
    finally:
        conn.close()


def update_question(qid: int, topic: str, stem: str, max_score: float,
                    points: List[RubricPoint]) -> None:
    """更新一道**已经存在**的题（题库功能，2026-10-04）。

    为什么需要它：以前每道题都是 AI 现出的、现插的，不存在"改一道已有的题"
    这件事。现在题目来自题库、导入时就已经在库里了，练习时教师又可能
    在首页改题面、改采分点 —— 这些改动得存回去。

    采分点是**整份替换**（先删后插），不做逐条 diff：
    教师的操作就是"增删改某几条"，diff 逻辑反而容易出边界问题。
    """
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE questions SET topic=?, stem=?, max_score=? WHERE id=?",
            (topic, stem, float(max_score), int(qid)),
        )
        conn.execute("DELETE FROM rubric_points WHERE question_id=?", (int(qid),))
        conn.executemany(
            "INSERT INTO rubric_points (question_id, seq, text, score)"
            " VALUES (?,?,?,?)",
            [(int(qid), p.seq, p.text, float(p.score)) for p in points],
        )
        conn.commit()
    finally:
        conn.close()


def create_paper(question_id: int, repeats: int) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (question_id, created_at, repeats, status)"
            " VALUES (?,?,?,?)",
            (question_id, _now(), repeats, "grading"),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def save_students(paper_id: int, students: List[Student]) -> None:
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT INTO students (paper_id, seq, ability, answer, design_score)"
            " VALUES (?,?,?,?,?)",
            [
                (paper_id, s.seq, s.ability, s.answer, s.design_score)
                for s in students
            ],
        )
        conn.commit()
    finally:
        conn.close()


def save_ai_scores(paper_id: int, ai_map: Dict[int, AiScore]) -> None:
    conn = get_conn()
    try:
        rows = []
        for seq, a in ai_map.items():
            detail = json.dumps(
                [
                    {
                        "point_seq": p.point_seq,
                        "text": p.text,
                        "score": p.score,
                        "level": p.level,
                        "agreement": p.agreement,
                        "reason": p.reason,
                        "evidence": p.evidence,
                    }
                    for p in a.points
                ],
                ensure_ascii=False,
            )
            rows.append(
                (paper_id, seq, a.total, a.confidence, a.score_range, a.comment, detail)
            )
        conn.executemany(
            "INSERT INTO ai_scores"
            " (paper_id, student_seq, total, confidence, score_range, comment, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def save_teacher_grade(paper_id: int, student_seq: int, score: float) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO teacher_grades (paper_id, student_seq, score, graded_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(paper_id, student_seq)"
            " DO UPDATE SET score=excluded.score, graded_at=excluded.graded_at",
            (paper_id, student_seq, score, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_teacher_grades(paper_id: int) -> Dict[int, float]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT student_seq, score FROM teacher_grades WHERE paper_id=?",
            (paper_id,),
        ).fetchall()
        return {int(r["student_seq"]): float(r["score"]) for r in rows}
    finally:
        conn.close()


def set_paper_status(paper_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE papers SET status=? WHERE id=?", (status, paper_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 耗时统计（2026-10-04 新增）
# ---------------------------------------------------------------------------

def save_timing(paper_id: int, t_question=None, t_answer=None,
                t_grade=None, t_total=None, api_calls=None) -> None:
    """把各步骤耗时写进 papers 表。字段都是可选的，缺的就留 NULL。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET t_question=?, t_answer=?, t_grade=?, "
            "t_total=?, api_calls=? WHERE id=?",
            (t_question, t_answer, t_grade, t_total, api_calls, paper_id),
        )
        conn.commit()
    finally:
        conn.close()


def load_timing(paper_id: int) -> dict:
    """读某次批改的耗时；老存档没有这些数据时各项是 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT t_question, t_answer, t_grade, t_total, api_calls "
            "FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        if not row:
            return {}
        return {k: row[k] for k in row.keys()}
    finally:
        conn.close()


def timing_history(limit: int = 20) -> List[dict]:
    """最近几次批改的耗时（新的在前），用来算"平均要多久"。

    只取记了 t_total 的那些，老存档（NULL）自动跳过。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, created_at, t_question, t_answer, t_grade, t_total, api_calls "
            "FROM papers WHERE t_total IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def _load_question(conn, qid: int) -> Optional[Question]:
    """读一道题。**题目行被删了就返回 None**（不是崩溃）。

    ⚠ 2026-10-05 实测踩过：教师把题库 txt 改名后重导，旧的两道题行被删，
       但引用它们的 papers 存档还在。教师点开那份历史记录时，
       `row["stem"]` 就是 TypeError → 整个应用崩掉。
       所以这里返回 None，由调用方决定怎么提示。
    """
    row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not row:
        log.warning("存档引用的题目 id=%s 已不存在（题库重导后旧题被清掉了）",
                    qid)
        return None
    points = [
        RubricPoint(seq=int(p["seq"]), text=p["text"], score=float(p["score"]))
        for p in conn.execute(
            "SELECT * FROM rubric_points WHERE question_id=? ORDER BY seq", (qid,)
        ).fetchall()
    ]
    # 题库来的题，材料存在 qbank_files 里（一份材料可能被多个小问共用），
    # 这里顺带取出来。AI 随机出题的老题没有这行记录，取到空串。
    material = ""
    # 同理，【评分说明】是教师自己写的判分硬约束，也要跟着题目取出来。
    note = ""
    try:
        m = conn.execute(
            "SELECT f.raw_text, fq.sub_note FROM qbank_file_questions fq "
            "JOIN qbank_files f ON f.id = fq.file_id WHERE fq.qid=?",
            (qid,),
        ).fetchone()
        if m:
            material = _material_from_raw(m["raw_text"] or "")
            note = m["sub_note"] or ""
    except sqlite3.Error:
        pass
    return Question(
        id=qid,
        subject=row["subject"],
        topic=row["topic"],
        stem=row["stem"],
        max_score=float(row["max_score"]),
        points=points,
        material=material,
        note=note,
    )


def _material_from_raw(raw: str) -> str:
    """从原始 txt 里取出【材料】那一段。

    存的是原文，这里只做"抠出材料段"，不改一个字。
    """
    if not raw:
        return ""
    import re

    m = re.search(r"[【\[（(]\s*材料\s*[】\]）)]\s*(.+?)(?=\n\s*[【\[（(]|\Z)",
                  raw, re.S)
    return m.group(1).strip() if m else ""


def _load_ai_scores(conn, paper_id: int, question: Question) -> Dict[int, AiScore]:
    from core.models import PointResult

    out: Dict[int, AiScore] = {}
    for r in conn.execute(
        "SELECT * FROM ai_scores WHERE paper_id=?", (paper_id,)
    ).fetchall():
        detail = json.loads(r["detail"] or "[]")
        points = [
            PointResult(
                point_seq=d.get("point_seq"),
                text=d.get("text", ""),
                score=float(d.get("score", 0)),
                level=int(d.get("level", 0)),
                agreement=float(d.get("agreement", 0)),
                reason=d.get("reason", ""),
                evidence=d.get("evidence", ""),
            )
            for d in detail
        ]
        out[int(r["student_seq"])] = AiScore(
            student_seq=int(r["student_seq"]),
            total=float(r["total"]),
            confidence=float(r["confidence"]),
            score_range=float(r["score_range"]),
            points=points,
            comment=r["comment"] or "",
        )
    return out


def load_paper(paper_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        p = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not p:
            return None
        question = _load_question(conn, int(p["question_id"]))
        if question is None:
            # 题目行被删了（题库重导），但这份存档和学生的答卷还在。
            # 不返回 None —— 那样教师连自己批过的卷子都看不到了。
            # 造一个占位题：界面能显示，学生答卷和 AI 判定照常看得到。
            log.warning("第 %s 份存档的题目已不存在，用占位题打开", paper_id)
            question = Question(
                subject="地理", topic="（题目已被删除）",
                stem="这份存档引用的题目已不存在"
                     "（多半是你把题库里的 txt 改名后重新导入，旧题被清理掉了）。"
                     "下面的学生答卷和AI 判定仍然完整。",
                max_score=0, points=[],
            )
        students = [
            Student(
                id=int(s["id"]),
                seq=int(s["seq"]),
                ability=s["ability"],
                answer=s["answer"],
                design_score=float(s["design_score"] or 0),
            )
            for s in conn.execute(
                "SELECT * FROM students WHERE paper_id=? ORDER BY seq", (paper_id,)
            ).fetchall()
        ]
        ai = _load_ai_scores(conn, paper_id, question)
        teacher = get_teacher_grades(paper_id)
        return {
            "paper_id": paper_id,
            "question": question,
            "students": students,
            "ai": ai,
            "teacher": teacher,
            "created_at": p["created_at"],
            "repeats": int(p["repeats"] or 0),
            "status": p["status"],
        }
    finally:
        conn.close()


def list_papers(limit: int = 50) -> List[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.created_at, p.status, p.repeats,
                   q.subject, q.topic, q.stem, q.max_score,
                   (SELECT COUNT(*) FROM students s WHERE s.paper_id=p.id) AS s_cnt,
                   (SELECT COUNT(*) FROM teacher_grades t WHERE t.paper_id=p.id) AS t_cnt
            FROM papers p LEFT JOIN questions q ON q.id=p.question_id
            ORDER BY p.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ===========================================================================
# 题库（2026-10-04 新增，配套 import_questions.py）
#
# 数据形状：一个 txt 里可能有多个【小问N】，**每个小问拆成一道独立的题**
# （题目记录 + 它自己的采分点），但**共享同一份材料**。
# 所以是"一个文件 → N 道题"的一对多关系：
#     qbank_files           一份材料（一个 txt）
#     qbank_file_questions  这份材料拆出来的每一道题（各带 qid）
#
# 抽题按 qid 抽，所以两个小问都能被抽到、各自独立走完整流程。
# ===========================================================================

def save_bank_file(filename: str, raw_text: str, sha1: str,
                   parse_status: str, n_subs: int, analysis: str = "",
                   has_answer: bool = True, max_score_source: str = "",
                   warnings: str = "") -> int:
    """记录一个 txt 已导入（按文件名判重）。返回 file_id。"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO qbank_files (filename, raw_text, sha1, imported_at,"
            " parse_status, n_subs, analysis, has_answer, max_score_source,"
            " warnings) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(filename) DO UPDATE SET"
            "   raw_text=excluded.raw_text, sha1=excluded.sha1,"
            "   imported_at=excluded.imported_at,"
            "   parse_status=excluded.parse_status, n_subs=excluded.n_subs,"
            "   analysis=excluded.analysis, has_answer=excluded.has_answer,"
            "   max_score_source=excluded.max_score_source,"
            "   warnings=excluded.warnings",
            (filename, raw_text, sha1, _now(), parse_status, n_subs, analysis,
             1 if has_answer else 0, max_score_source, warnings),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM qbank_files WHERE filename=?", (filename,)
        ).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def link_bank_question(file_id: int, sub_no: int, qid: int,
                       sub_stem: str = "", sub_score: float = 0.0,
                       sub_note: str = "") -> None:
    """把"第 sub_no 小问"和"questions 表里的 qid"关联起来。

    sub_note = 教师写的【评分说明】（2026-10-05）。
    它必须存在映射表里而不是 questions 表，因为一份材料下每个小问
    的说明是不同的。
    """
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO qbank_file_questions (file_id, sub_no, qid, sub_stem,"
            " sub_score, sub_note) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(file_id, sub_no) DO UPDATE SET"
            "   qid=excluded.qid, sub_stem=excluded.sub_stem,"
            "   sub_score=excluded.sub_score, sub_note=excluded.sub_note",
            (file_id, int(sub_no), int(qid), sub_stem, float(sub_score),
             sub_note or ""),
        )
        conn.commit()
    finally:
        conn.close()


def get_file_imported(filename: str):
    """这个文件之前导过吗？导过返回 (file_id, n_subs)，没导过返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, n_subs FROM qbank_files WHERE filename=?", (filename,)
        ).fetchone()
        if not row:
            return None
        return int(row["id"]), int(row["n_subs"] or 1)
    finally:
        conn.close()


def find_file_by_sha1(sha1: str) -> Optional[dict]:
    """按内容指纹找已导入的文件（文件名可以不同）。

    用途（2026-10-05）：教师把 19_xxx.txt 改名成 01_xxx.txt，
    文件名判重抓不到，会导入出两份一样的题。用内容 sha1 就能认出来。
    """
    if not sha1:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, filename, n_subs FROM qbank_files WHERE sha1=?",
            (sha1,),
        ).fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "filename": row["filename"],
                "n_subs": int(row["n_subs"] or 1)}
    finally:
        conn.close()


def clear_bank_questions(file_id: int, force: bool = False) -> dict:
    """重导之前先清掉这个文件之前拆出来的题目（连同 questions / rubric_points）。

    ⚠ 会真的删 questions / rubric_points 里的行 —— 这些行是这个 txt 专属的
    （AI 随机出题时代没有它们），所以删除安全。但先删映射表，
    免得留下指向已删题目的悬空记录。

    force=False（默认）：有练习记录的题**跳过不删** —— 那是用户的练习历史。
    force=True：连练习记录一起清掉。
        ⚠ 只在"教师把 txt 改了名/改了内容，明确要重来"时用。
          副作用：那几道题的练习计数归零；若有存档引用这些 qid，
          存档还在（papers 行不动），但从历史记录点开时题面会显示不出来。
          **调用前务必先备份 data/grader.db。**

    返回 {"deleted": [...], "kept": [...]}，让调用方能告诉用户发生了什么。
    """
    deleted: List[int] = []
    kept: List[int] = []
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT qid FROM qbank_file_questions WHERE file_id=?", (file_id,)
        ).fetchall()
        qids = [int(r["qid"]) for r in rows if r["qid"]]
        for qid in qids:
            used = conn.execute(
                "SELECT COUNT(*) FROM qbank_practice WHERE qid=?", (qid,)
            ).fetchone()[0]
            if used and not force:
                kept.append(qid)
                continue
            conn.execute("DELETE FROM rubric_points WHERE question_id=?", (qid,))
            conn.execute("DELETE FROM qbank_practice WHERE qid=?", (qid,))
            conn.execute("DELETE FROM questions WHERE id=?", (qid,))
            deleted.append(qid)
        # ⚠ 只有"确实删了题目"才清映射。
        #   全被跳过的时侯（kept 装满了）必须把映射留着 ——
        #   否则题目还在 questions 表里、却再也没人认领它，
        #   变成抽不到的孤儿（实测踩过：重跑一次 clear 就全找不到了）。
        if deleted or not kept:
            conn.execute("DELETE FROM qbank_file_questions WHERE file_id=?",
                         (file_id,))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": deleted, "kept": kept}


def find_bank_file_by_qid(qid: int) -> Optional[int]:
    """这个 qid 属于题库里的哪个文件（不是题库题就返回 None）。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT file_id FROM qbank_file_questions WHERE qid=?", (qid,)
        ).fetchone()
        return int(row["file_id"]) if row else None
    finally:
        conn.close()


def list_bank_files() -> List[dict]:
    """题库里所有已导入的文件。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, filename, imported_at, parse_status, n_subs,"
            " has_answer, max_score_source, warnings"
            " FROM qbank_files ORDER BY id DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def bank_questions_count() -> int:
    """题库里一共有多少道可抽的题（= 各文件拆出的小问总数）。

    ⚠ 只算**题目行还在**的那些：重导后可能留下指向已删题目的残留映射，
      那些算进去会让进度永远算不对（显示"还剩1 道"却抽不出来）。
    """
    conn = get_conn()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM qbank_file_questions fq "
            "JOIN questions q ON q.id = fq.qid "
            "WHERE fq.qid IS NOT NULL"
        ).fetchone()[0])
    finally:
        conn.close()


def load_bank_questions() -> List[Question]:
    """题库里所有可抽的题（含材料），按题目 id 升序。

    抽题时用这个：每道题都带着自己的设问、满分、采分点和**材料**。
    ⚠ 材料不在 questions 表里（同一份材料被多个小问共用），
       所以要从 qbank_file_questions → qbank_files 关联取。
    """
    conn = get_conn()
    try:
        qids = [int(r["qid"]) for r in conn.execute(
            "SELECT qid FROM qbank_file_questions WHERE qid IS NOT NULL "
            "ORDER BY qid"
        ).fetchall()]
        out: List[Question] = []
        for q in qids:
            # 题目行可能已被清掉（重导后的残留映射），跳过而不是崩
            item = _load_question(conn, q)
            if item is not None:
                out.append(item)
        return out
    finally:
        conn.close()


def load_bank_question(qid: int) -> Optional[Question]:
    """按 qid 读一道题库里的题（含材料）。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM qbank_file_questions WHERE qid=?", (qid,)
        ).fetchone()
        if not row:
            return None
        return _load_question(conn, qid)
    finally:
        conn.close()
