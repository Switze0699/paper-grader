"""SQLite 数据库：建表与连接。"""

from __future__ import annotations

import sqlite3

from core import paths

ROOT = paths.user_root()
DATA_DIR = paths.writable("data")
DB_PATH = DATA_DIR / "grader.db"

# papers 表后来加的列（2026-10-04 加耗时统计）。
# CREATE TABLE IF NOT EXISTS 对**已存在的表不会补列**，所以老存档必须靠
# _ensure_columns() 补，否则查 papers.t_total 会报 "no such column"。
NEW_PAPER_COLUMNS = [
    ("t_question", "REAL"),
    ("t_answer", "REAL"),
    ("t_grade", "REAL"),
    ("t_total", "REAL"),
    ("api_calls", "INTEGER"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT,
    topic TEXT,
    stem TEXT,
    max_score REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS rubric_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER,
    seq INTEGER,
    text TEXT,
    score REAL
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER,
    created_at TEXT,
    repeats INTEGER,
    status TEXT,
    -- ⚠ 2026-10-04 新增：每一步的耗时（秒）。老存档没有这几列，
    --   由 _ensure_columns() 补上，所以旧数据照样能用（补 NULL）。
    --   t_question = 出题 / t_answer = 生成答卷 / t_grade = AI 阅卷
    --   t_total    = 从点「让 AI 出一道新题」到存档的总耗时
    --   api_calls  = 这一轮总共发了多少次 API 请求（算额度用）
    t_question REAL,
    t_answer REAL,
    t_grade REAL,
    t_total REAL,
    api_calls INTEGER
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    seq INTEGER,
    ability TEXT,
    answer TEXT,
    design_score REAL
);

CREATE TABLE IF NOT EXISTS ai_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    student_seq INTEGER,
    total REAL,
    confidence REAL,
    score_range REAL,
    comment TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS teacher_grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    student_seq INTEGER,
    score REAL,
    graded_at TEXT,
    UNIQUE(paper_id, student_seq)
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补上后来新增的列。

    `CREATE TABLE IF NOT EXISTS` 遇到同名表就跳过，不会补列；
    所以老存档（这次加耗时字段之前建的）必须在这里补，否则查询会报错。
    """
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    except sqlite3.Error:
        return
    for name, decl in NEW_PAPER_COLUMNS:
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
        except sqlite3.Error:
            pass   # 多个进程同时补列时可能撞车，忽略即可
    try:
        conn.commit()
    except sqlite3.Error:
        pass


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn
