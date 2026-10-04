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

-- ===========================================================================
-- 题库（2026-10-04 新增）
--
-- 以前题目是 AI 随机出的，现在改成"教师自己维护 题库/*.txt，导入进来"。
--
-- ⚠ 为什么单独建表、不往 questions 表加字段：
--   这套逻辑是新的，万一有问题，把这三张表 DROP 掉就回到现在的状态，
--   你已���的 42 份存档（questions / papers / ai_scores…）完全不受影响。
-- ===========================================================================

-- ① 导入过的 txt 原始文件。filename 唯一 → 靠它判重、跳过重复导入
--    ⚠ 这里**不存 qid** 了：一个 txt 可能拆成多个小问 → 多道题 → 多个 qid，
--    关系放在 qbank_file_questions 里（一对多）。存单个 qid 表达不了。
CREATE TABLE IF NOT EXISTS qbank_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE NOT NULL,
    raw_text TEXT,
    sha1 TEXT,
    imported_at TEXT,
    parse_status TEXT,          -- ok / need_review / failed
    n_subs INTEGER DEFAULT 1,   -- 这个文件拆出几道题
    analysis TEXT,              -- 题目解析（原文带的）
    has_answer INTEGER,         -- 原文里有没有答案
    max_score_source TEXT,      -- written（原文写了）/ inferred（点数×2推断）
    warnings TEXT
);

-- ①-b 一个 txt 拆出来的每一道题。★ 多小问支持的关键
--    例：19_盐风化.txt 里有【小问1】【小问2】→ 拆成 2 行，qid 各不相同，
--    但 material_id 指向同一个材料（这里用 qbank_files.id 表示）。
--    抽题时按 qid 抽，两道题都能被抽到、各自独立走完整流程。
CREATE TABLE IF NOT EXISTS qbank_file_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,   -- 指向 qbank_files.id（= 哪一份材料）
    sub_no INTEGER NOT NULL,    -- 原文件里的小问号（1、2…）
    qid INTEGER,                -- 存进 questions 表后的 id（真正用于抽题）
    sub_stem TEXT,              -- 这个小问的设问（冗余存一份，方便显示）
    sub_score REAL,             -- 这个小问的满分（冗余）
    UNIQUE(file_id, sub_no)
);

-- ② 练习轮次：保证每道题都抽到一次，才允许重开下一轮
CREATE TABLE IF NOT EXISTS qbank_rounds (
    round_no INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT
);

-- ③ 抽题记录。UNIQUE(qid, round_no) 是关键：
--    一轮之内同一道题只能被抽到一次
CREATE TABLE IF NOT EXISTS qbank_practice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qid INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    paper_id INTEGER,
    practiced_at TEXT,
    UNIQUE(qid, round_no)
);
"""

# qbank_files 后来不再用 qid 列（改成一对多的 qbank_file_questions），
# 但老库里可能已经有这列，留着无害。
NEW_QBANK_FILE_COLUMNS = [
    ("n_subs", "INTEGER DEFAULT 1"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补上后来新增的列。

    `CREATE TABLE IF NOT EXISTS` 遇到同名表就跳过，不会补列；
    所以老存档（这次加耗时字段之前建的）必须在这里补，否则查询会报错。
    """
    for table, cols in (("papers", NEW_PAPER_COLUMNS),
                        ("qbank_files", NEW_QBANK_FILE_COLUMNS)):
        try:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not have:
            continue          # 表还没建（正常情况，SCHEMA 会建）
        for name, decl in cols:
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.Error:
                pass       # 多进程同时补列可能撞车，忽略即可
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
