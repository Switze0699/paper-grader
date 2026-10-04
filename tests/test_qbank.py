"""题库抽题 + 轮次重置的独立自测（2026-10-04）。

单独跑：.venv/Scripts/python.exe tests/test_qbank.py

⚠ 全程用**临时数据库**（tests/_tmp_qbank.db），跑完删掉。
   绝不碰你 data/grader.db 里的 43 份真实存档和练习记录。
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_DB = ROOT / "tests" / "_tmp_qbank.db"

FAILED: list = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  通过  " if ok else "  ✗ 失败 ") + name + (f"　{extra}" if extra else ""))
    if not ok:
        FAILED.append(name)


def _fresh_db(n_questions: int = 3) -> None:
    """建一个干净的临时库，并塞进 n_questions 道题库题。"""
    if TMP_DB.exists():
        TMP_DB.unlink()

    import storage.db as db
    db.DB_PATH = TMP_DB                      # 让所有函数都写到这个临时库

    # 直接塞题库数据：1 个文件、n 个小问 → n 道题，共享同一份材料
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO qbank_files (filename, raw_text, n_subs, parse_status)"
            " VALUES (?,?,?,?)",
            ("测试题.txt",
             "【材料】测试材料：某地位于我国东南丘陵。\n"
             "【小问1】\n【设问】分析地形起伏。\n"
             "【小问2】\n【设问】分析气候特征。\n"
             "【小问3】\n【设问】分析河流发育。",
             n_questions, "ok"),
        )
        file_id = conn.execute("SELECT id FROM qbank_files").fetchone()["id"]
        for i in range(1, n_questions + 1):
            cur = conn.execute(
                "INSERT INTO questions (subject, topic, stem, max_score, created_at)"
                " VALUES (?,?,?,?,?)",
                ("地理", "测试", f"设问{i}的正文", 6, "2026-10-04"),
            )
            qid = cur.lastrowid
            conn.execute(
                "INSERT INTO rubric_points (question_id, seq, text, score)"
                " VALUES (?,?,?,?)",
                (qid, 1, f"第{i}点：条件→机制", 2),
            )
            conn.execute(
                "INSERT INTO qbank_file_questions (file_id, sub_no, qid,"
                " sub_stem, sub_score) VALUES (?,?,?,?,?)",
                (file_id, i, qid, f"设问{i}的正文", 6),
            )
        conn.commit()
    finally:
        conn.close()


def test_empty_bank():
    print("\n[空题库]")
    from core import qbank

    TMP_DB.unlink(missing_ok=True)
    import storage.db as db
    db.DB_PATH = TMP_DB
    db.get_conn().close()

    r = qbank.pick()
    check("空题库抽题不会崩溃", r.question is None)
    check("给了能看懂的原因", "题库" in r.reason, r.reason)
    check("空题库进度是 0", qbank.progress()["total"] == 0)


def test_pick_returns_full_question():
    print("\n[抽到的题长什么样]")
    from core import qbank
    from core.models import Question

    _fresh_db(3)
    r = qbank.pick(seed=1)
    check("能抽到题", r.ok, r.reason)
    check("返回的是 Question 对象", isinstance(r.question, Question))
    q = r.question
    check("带着材料（★ 修复的那个坑）", "测试材料" in (q.material or ""),
          repr(q.material))
    check("full_stem() 里既有材料也有设问",
          "测试材料" in q.full_stem() and q.stem in q.full_stem())
    check("带着采分点", len(q.points) == 1, str(len(q.points)))
    check("带着满分 6 分", q.max_score == 6, str(q.max_score))
    check("带着真实 qid（能溯源）", isinstance(q.id, int), str(q.id))
    check("主题不是空的", bool(q.topic), repr(q.topic))


def test_no_repeat_in_one_round():
    print("\n[一轮之内不重复]")
    from core import qbank

    _fresh_db(3)
    got = []
    for i in range(3):
        r = qbank.pick(seed=100 + i)
        got.append(r.question.id)
    check("3 道题抽了 3 次，全是不同题", len(set(got)) == 3, str(got))

    p = qbank.progress()
    check("进度显示第 1 轮", p["round_no"] == 1, str(p))
    check("已练 3 道", p["done"] == 3, str(p))
    check("还剩 0 道", p["remaining"] == 0, str(p))
    check("标记为本轮已练完", p["finished"] is True, str(p))


def test_auto_next_round():
    print("\n[全部练完 → 自动开新一轮]")
    from core import qbank

    _fresh_db(3)
    for i in range(3):
        qbank.pick(seed=200 + i)

    # ⚠ 刚练满时progress() 显示的是"第 1 轮 3/3 已练完"，不是"第 2 轮 0/3"。
    #   这是故意的：教师得先看得见"这一轮练完了"，
    #   下一题才开新一轮（progress 只看不改，advance=False）。
    p = qbank.progress()
    check("刚练满时仍显示第 1 轮（让教师看得见练完了）",
          p["round_no"] == 1, str(p))
    check("刚练满时显示 3/3", p["done"] == 3, str(p))
    check("刚练满时 finished 为真", p["finished"] is True, str(p))

    # 真正再抽一次，才开新一轮
    r = qbank.pick(seed=300)
    check("下一题自动开第 2 轮", r.round_no == 2, str(r.round_no))
    # pick() 返回时这道题已经记进第 2 轮了，所以是 1/3 而不是 0/3
    p2 = qbank.progress()
    check("第 2 轮已练 1 道（刚抽的那道）",
          p2["round_no"] == 2 and p2["done"] == 1, str(p2))
    check("第 2 轮还剩 2 道", p2["remaining"] == 2, str(p2))
    check("第 1 轮练过的题可以再抽（允许重复）",
          r.question is not None and r.question.id in (1, 2, 3))


def test_seed_is_reproducible():
    print("\n[同 seed 结果可复现（测试用）]")
    from core import qbank

    _fresh_db(5)
    a = qbank.pick(seed=42).question.id
    _fresh_db(5)
    b = qbank.pick(seed=42).question.id
    check("同一个 seed 抽到同一道题", a == b, f"{a} vs {b}")


def test_link_paper():
    print("\n[练习记录能对上存档号]")
    from core import qbank
    from storage.db import get_conn

    _fresh_db(2)
    r = qbank.pick(seed=7)
    qbank.link_paper(r.question.id, r.round_no, 9999)

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT paper_id FROM qbank_practice WHERE qid=? AND round_no=?",
            (r.question.id, r.round_no),
        ).fetchone()
    finally:
        conn.close()
    check("存档号写进去了", row and row["paper_id"] == 9999, str(dict(row) if row else None))


def test_reset_all():
    print("\n[一键重置]")
    from core import qbank

    _fresh_db(3)
    for i in range(2):
        qbank.pick(seed=400 + i)
    check("重置前已练 2 道", qbank.progress()["done"] == 2, str(qbank.progress()))

    n = qbank.reset_all()
    check("返回删掉的条数", n == 2, str(n))
    p = qbank.progress()
    check("重置后回到第 1 轮", p["round_no"] == 1, str(p))
    check("重置后已练 0 道", p["done"] == 0, str(p))


def test_tolerates_shrinking_bank():
    print("\n[题库变小时不会卡死]")
    from core import qbank
    from storage.db import get_conn

    _fresh_db(3)
    for i in range(3):
        qbank.pick(seed=500 + i)          # 第 1 轮练满

    # 教师删掉 2 道题（连同 questions 行），题库从 3 道变成 1 道
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM qbank_practice WHERE qid NOT IN"
            " (SELECT qid FROM qbank_file_questions)"
        )
        conn.commit()
    finally:
        conn.close()

    p = qbank.progress()
    check("题库变小后进度仍能算出来", p["total"] >= 1, str(p))
    check("不会卡在同一轮出不了题", p["round_no"] >= 1, str(p))
    # 最关键的一句：删完题库后还能不能抽出题来
    r = qbank.pick(seed=600)
    check("题库变小后仍然抽得到题", r.ok, r.reason)


def test_scoring_note():
    print("\n[【评分说明】· 教师写的判分硬约束]")
    from core.qbank_parse import parse_file

    txt = (
        "【主题】测试\n"
        "【材料】某地位于东南丘陵，基岩为花岗岩。\n"
        "【小问1】\n"
        "【设问】简述形成过程。\n"
        "【分值】6\n"
        "【答案】甲；乙；丙（每点2分，共6分）\n"
        "【评分说明】只写出前两步的，总分不得超过4分。\n"
        "【小问2】\n"
        "【设问】判断依据。\n"
        "【分值】6\n"
        "【答案】丁；戊；己\n"
    )
    pf = parse_file(txt)
    check("解析成功", pf.ok, str(pf.warnings))
    rows = pf.dicts()
    check("小问1读到了评分说明",
          "不得超过4分" in rows[0]["note"], repr(rows[0]["note"]))
    check("小问2没有评分说明（不串味）",
          rows[1]["note"] == "", repr(rows[1]["note"]))

    # 近义标签也要认（教师可能写成别的名字）
    for alt in ("评分备注", "判分说明", "备注", "说明"):
        pf2 = parse_file(
            "【材料】M\n【小问1】\n【设问】S\n【答案】A；B\n"
            f"【{alt}】注意判分尺度\n"
        )
        check(f"认得【{alt}】",
              "判分尺度" in pf2.dicts()[0]["note"],
              repr(pf2.dicts()[0]["note"]))

    # ⚠ 关键：说明要真的发给 AI 阅卷官，且"没有说明时提示词一个字都不多"
    from core.models import Question, RubricPoint
    from services.grading_service import _build_prompts

    cfg = {"grading": {"temperature": 0.0}}
    withnote = Question(
        subject="地理", topic="测试", max_score=6, stem="简述形成过程。",
        note="只写出前两步的，总分不得超过4分。",
        points=[RubricPoint(1, "甲", 2)],
    )
    nonote = Question(
        subject="地理", topic="测试", max_score=6,
        stem="【材料】某河段位于湿润山区。\n【设问】分析水文特征。（8分）",
        points=[RubricPoint(1, "降水丰富", 2)],
    )
    _, u1 = _build_prompts(cfg, withnote, "答")
    _, u2 = _build_prompts(cfg, nonote, "答")
    check("评分说明发给了 AI 阅卷官",
          "不得超过4分" in u1 and "判分特别规定" in u1)
    check("没有说明时整块不出现",
          "判分特别规定" not in u2 and "特别规定" not in u2)
    check("没有说明时提示词不留多余空行",
          u2.startswith("【题目】\n【材料】"), repr(u2[:24]))
    check("老题提示词与从前一致（没有多余行）",
          u2.count("\n\n【评分细则") == 1, repr(u2[u2.find("【题目】"):][:60]))


def test_no_duplicate_on_rename():
    print("\n[改名后重导不会留下重复题]")
    from storage import repository as repo

    _fresh_db(2)
    check("临时库里有 2 道题", repo.bank_questions_count() == 2,
          str(repo.bank_questions_count()))

    # 模拟：按内容 sha1 能认出"只是改了名"
    from import_questions import sha1_of
    raw = "【材料】测试材料：某地位于我国东南丘陵。"
    sha = sha1_of(raw)
    conn = __import__("storage.db", fromlist=["get_conn"]).get_conn()
    conn.execute(
        "INSERT INTO qbank_files (filename, raw_text, sha1, n_subs)"
        " VALUES (?,?,?,1)", ("19_旧名.txt", raw, sha))
    conn.commit()
    conn.close()

    hit = repo.find_file_by_sha1(sha)
    check("按内容认出改过名的文件",
          hit and hit["filename"] == "19_旧名.txt", str(hit))
    check("sha1 找不到时返回 None", repo.find_file_by_sha1("") is None)

    # clear_bank_questions(force=True) 连练习记录一起清
    conn = __import__("storage.db", fromlist=["get_conn"]).get_conn()
    # ⚠ 要取"19_旧名.txt"那一行的 id，不是第一行 ——
    #   库里已经有 _fresh_db(2) 建的那个文件了
    fid = conn.execute(
        "SELECT id FROM qbank_files WHERE filename='19_旧名.txt'"
    ).fetchone()["id"]
    # 它自己没有拆出题（上面只插了 qbank_files 行），
    # 所以给它挂一道题，才能测 clear 的行为
    cur = conn.execute(
        "INSERT INTO questions (subject, topic, stem, max_score, created_at)"
        " VALUES ('地理','测试','旧题',6,'x')")
    qid_old = cur.lastrowid
    conn.execute(
        "INSERT INTO qbank_file_questions (file_id, sub_no, qid, sub_stem,"
        " sub_score) VALUES (?,1,?,?,6)", (fid, qid_old, "旧题"))
    conn.execute("INSERT INTO qbank_practice (qid, round_no) VALUES (?,1)",
                 (qid_old,))
    conn.commit()
    conn.close()

    # 不 force：跳过不删（保护练习历史）
    r1 = repo.clear_bank_questions(fid)
    check("默认不删有练习记录的题", r1["kept"] == [qid_old] and not r1["deleted"],
          str(r1))
    # force：连练习记录一起清
    r2 = repo.clear_bank_questions(fid, force=True)
    check("force=True 时连练习记录一起清",
          r2["deleted"] == [qid_old] and not r2["kept"], str(r2))
    check("清完这题就不在题库里了",
          repo.bank_questions_count() == 2, str(repo.bank_questions_count()))


def main() -> None:
    print("=" * 46)
    print(" 题库抽题 · 独立自测（用临时库，不碰真实存档）")
    print("=" * 46)
    try:
        test_empty_bank()
        test_pick_returns_full_question()
        test_no_repeat_in_one_round()
        test_auto_next_round()
        test_seed_is_reproducible()
        test_link_paper()
        test_reset_all()
        test_tolerates_shrinking_bank()
        test_scoring_note()
        test_no_duplicate_on_rename()
    finally:
        # 收尾：删掉临时库，把 DB_PATH 还原成真的那个
        import storage.db as db
        db.DB_PATH = ROOT / "data" / "grader.db"
        TMP_DB.unlink(missing_ok=True)

    print("\n" + "=" * 46)
    if FAILED:
        print(f" 有 {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        sys.exit(1)
    print(" 全部通过")


if __name__ == "__main__":
    main()
