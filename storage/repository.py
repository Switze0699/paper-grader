"""数据读写：存档、读取历史、取回某次练习的全部数据。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from core.models import AiScore, Question, RubricPoint, Student
from storage.db import get_conn


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


def _load_question(conn, qid: int) -> Question:
    row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    points = [
        RubricPoint(seq=int(p["seq"]), text=p["text"], score=float(p["score"]))
        for p in conn.execute(
            "SELECT * FROM rubric_points WHERE question_id=? ORDER BY seq", (qid,)
        ).fetchall()
    ]
    return Question(
        id=qid,
        subject=row["subject"],
        topic=row["topic"],
        stem=row["stem"],
        max_score=float(row["max_score"]),
        points=points,
    )


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


# ---------------------------------------------------------------------------
# 题库（2026-10-04 新增，配套 import_questions.py）
# ---------------------------------------------------------------------------
# 抽题、轮次重置这些逻辑放在 core/qbank.py，这里只管存和取。

def save_bank_file(filename: str, raw_text: str, sha1: str,
                   parse_status: str, qid: int, analysis: str = "",
                   has_answer: bool = True, max_score_source: str = "",
                   warnings: str = "") -> int:
    """记录一个 txt 已经导入过（按文件名判重）。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO qbank_files (filename, raw_text, sha1, imported_at,"
            " parse_status, qid, analysis, has_answer, max_score_source, warnings)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(filename) DO UPDATE SET"
            "   raw_text=excluded.raw_text, sha1=excluded.sha1,"
            "   imported_at=excluded.imported_at, parse_status=excluded.parse_status,"
            "   qid=excluded.qid, analysis=excluded.analysis,"
            "   has_answer=excluded.has_answer,"
            "   max_score_source=excluded.max_score_source, warnings=excluded.warnings",
            (filename, raw_text, sha1, _now(), parse_status, qid, analysis,
             1 if has_answer else 0, max_score_source, warnings),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def get_file_imported_qid(filename: str):
    """这个文件之前导过吗？导过就返回 qid，没导过返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT qid FROM qbank_files WHERE filename=?", (filename,)
        ).fetchone()
        return row["qid"] if row else None
    finally:
        conn.close()


def list_bank_files() -> List[dict]:
    """题库里所有已导入的题目（按导入时间倒序）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM qbank_files ORDER BY id DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()
