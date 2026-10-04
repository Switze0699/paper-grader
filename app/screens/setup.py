"""第一步：出题 + 编辑评分细则 + 生成答卷 + AI 阅卷。"""

from __future__ import annotations

import logging
import time

import flet as ft

from app import theme
from core import qbank
from core.models import RubricPoint
from core.pipeline import build_class, build_question, persist, run_grading
from core.timing import (Stopwatch, get_api_calls, reset_api_calls,
                         timing_summary_line)

log = logging.getLogger(__name__)

REPEAT_OPTIONS = [(1, "快速 1 次"), (3, "标准 3 次"), (5, "严格 5 次")]


def _small_btn(text: str, on_click, color: str = theme.SLATE) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=12, color="#FFFFFF"),
        bgcolor=color,
        border_radius=6,
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        on_click=on_click,
        ink=True,
    )


def _header(app) -> ft.Container:
    return ft.Container(
        content=ft.Column(
            [
                ft.Text("批改模拟器", size=20, color=theme.TEXT,
                        weight=ft.FontWeight.BOLD),
                ft.Text("AI 出卷 → 生成学生答卷 → AI 阅卷定基准 → 你盲评 → 对比报告",
                        size=13, color=theme.MUTED),
                ft.Row(
                    [
                        theme.button("历史记录", lambda e=None: app.go_history(),
                                     bgcolor=theme.SLATE),
                        theme.button("手机访问", lambda e=None: app.go_mobile(),
                                     bgcolor=theme.GREEN),
                    ],
                    spacing=8,
                ),
            ],
            spacing=6,
        ),
        bgcolor=theme.CARD2,
        padding=16,
        border_radius=10,
        border=ft.Border.all(1, theme.BORDER),
    )


def _no_key_card(app) -> ft.Container:
    """没有密钥时的提示。

    桌面上可以让用户改 .env；手机上改不了文件，
    所以这里直接给一个输入框，填完点「保存并继续」就能用。
    """
    st = app.state
    if st.get("_key_field") is None:
        st["_key_field"] = ft.TextField(
            hint_text="在这里粘贴你的 API 密钥",
            password=True, can_reveal_password=True,
            text_size=14,
        )

    def _save(e=None):
        from core.config import save_api_key
        key = (st["_key_field"].value or "").strip()
        if not key:
            return
        save_api_key(key)
        app.api_key = key
        st["_key_field"] = None
        app.go_setup()
        app.page.snack_bar = ft.SnackBar(ft.Text("密钥已保存，可以开始出题了"))
        app.page.update()

    return theme.card(
        ft.Column(
            [
                ft.Text("还没有配置 AI 密钥", size=17, color=theme.RED,
                        weight=ft.FontWeight.BOLD),
                ft.Text(
                    "1. 到 https://open.bigmodel.cn 登录，在「API Keys」里复制密钥\n"
                    "2. 粘贴到下面的输入框，点「保存并继续」",
                    size=14, color=theme.TEXT,
                ),
                st["_key_field"],
                ft.Row(
                    [theme.button("保存并继续", _save)],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Text(
                    "电脑上也可以用记事本打开项目文件夹里的 .env，"
                    "把 ZHIPU_API_KEY= 后面换成密钥，保存后重开程序。",
                    size=12, color=theme.SUB,
                ),
            ],
            spacing=10,
        ),
        bgcolor=theme.CARD2,
    )


def _start_practice(app) -> None:
    """从题库随机抽一道题，填进 st['question']，然后刷新首页。

    抽题失败（题库空）时给一句人话提示，不弹技术性报错。
    """
    st = app.state
    try:
        picked = qbank.pick()
    except Exception as e:  # noqa: BLE001
        log.exception("抽题失败")
        app.show_error(f"抽题失败：{e}")
        return

    if not picked.ok:
        app.show_error(picked.reason or "题库里没有可用题目。")
        return

    q = picked.question
    st["question"] = q
    st["_stem_field"] = None          # 让render 重建
    st["_point_fields"] = []
    st["_qbank_qid"] = q.id
    st["_qbank_round"] = picked.round_no
    # 计时：题库抽题是本地操作，不花时间，但要归零，
    # 否则会沿用上一轮"AI 出题"的耗时，让报告里的统计对不上。
    st["_t_question"] = 0.0
    st["_q_calls"] = 0
    _refresh(app)


def _idle_card(app) -> ft.Container:
    """首页主界面。

    ★ 主入口是「开始练习」——从你自己的题库随机抽题。
      「让 AI 出一道新题」保留成下面的次要按钮：题库跑光了、或者临时想
      练个新题时有个后路（它会花一次 API 调用出一套新题+细则）。
    """
    p = qbank.progress()
    total = p["total"]

    if total <= 0:
        body = [
            ft.Text("题库还是空的", size=17, color=theme.TEXT,
                    weight=ft.FontWeight.BOLD),
            ft.Text(
                "把试题的 .txt 放进项目里的「题库」文件夹，\n"
                "然后运行 import_questions.py 导入。\n"
                "题库里的题会按轮次随机抽给你，一轮练完自动从头再来。",
                size=14, color=theme.SUB,
            ),
        ]
    else:
        if p["finished"]:
            prog = f"第 {p['round_no']} 轮已全部练完（{p['done']}/{p['total']}）。\n下一题会自动开新一轮。"
        else:
            prog = (f"题库共 {p['total']} 道题　·　"
                    f"第 {p['round_no']} 轮已练 {p['done']} 道，"
                    f"还剩 {p['remaining']} 道")
        body = [
            ft.Text("准备好了吗？", size=17, color=theme.TEXT,
                    weight=ft.FontWeight.BOLD),
            ft.Text(prog, size=14, color=theme.SUB),
            ft.Row(
                [theme.button("开始练习",
                              lambda e=None: _start_practice(app))],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.Text(
                "抽到的题会连材料、设问、评分细则一起给你，\n"
                "你核对细则后就能生成学生答卷。",
                size=12, color=theme.MUTED,
            ),
        ]

    return theme.card(
        ft.Column(
            [
                *body,
                ft.Divider(height=1, color=theme.BORDER),
                # 次要入口：题库跑光了/想练新题时的后路
                ft.Row(
                    [theme.button("让 AI 出一道新题（备用）",
                                  lambda e=None: app.page.run_task(_do_question, app),
                                  bgcolor=theme.SLATE)],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.CARD2,
        padding=32,
    )


def _note_card(app) -> ft.Container:
    """教师自己写的【评分说明】——显示出来，并确认它会发给 AI 阅卷。

    ⚠ 2026-10-05 新增。以前这条只在 txt 里，解析时被当成陌生标签丢掉，
       教师在界面上完全看不到自己写了什么、也不知道 AI 有没有收到。
    """
    q = app.state.get("question")
    if q is None or not (q.note or "").strip():
        return ft.Container()          # 空容器 = 不占位置、不显示
    return theme.card(
        ft.Column(
            [
                theme.label("【评分说明】你写的判分规定，AI 阅卷时会收到并遵守"),
                ft.Container(
                    content=ft.Text(q.note, size=14, color=theme.TEXT,
                                    selectable=True),
                    bgcolor=theme.CARD2,
                    border=ft.Border.all(1, theme.BLUE),
                    border_radius=6,
                    padding=12,
                    width=999999,
                ),
                ft.Text(
                    "例如「总分不得超过 4 分」这类封顶规定，AI 会按它执行。",
                    size=12, color=theme.MUTED,
                ),
            ],
            spacing=8,
        ),
        padding=14,
    )


def _material_card(app) -> ft.Container:
    """题库来的题，材料单独显示成一张只读卡片。

    ⚠ 2026-10-04 为什么材料不塞进下面的编辑框：
      那个框回写时是整体赋给 q.stem 的（见 _sync_fields）。
      一旦把"材料 + 设问"一起显示进去，用户随便改一个字再保存，
      材料就被并进 stem 里了 —— 再刷新一次，材料会重复出现。
      所以：材料只读、单独一张卡；要改材料就改 题库/*.txt 再重新导入。
      AI 随机出题的老题 material 为空，这张卡整个不显示。
    """
    q = app.state.get("question")
    if q is None or not (q.material or "").strip():
        return ft.Container()          # 空容器 = 不占位置、不显示
    return theme.card(
        ft.Column(
            [
                theme.label("【材料】来自题库，修改请改 题库 里的 txt 后重新导入"),
                ft.Container(
                    content=ft.Text(q.material, size=14, color=theme.TEXT,
                                    selectable=True),
                    width=999999,
                ),
            ],
            spacing=8,
        ),
        padding=14,
    )


def _switch_question(app) -> None:
    """「换一道题」。

    题库来的题 → 换下一道题库题（本地，免费）。
    AI 出的题 → 换一道 AI 新题（这本来就是要花 API 的）。
    """
    if app.state.get("_qbank_qid"):
        _start_practice(app)
    else:
        app.page.run_task(_do_question, app)


def _question_card(app, stem_field: ft.TextField) -> ft.Container:
    from_bank = bool(app.state.get("_qbank_qid"))
    btn_text = "换一道题库里的题" if from_bank else "换一道题"
    return theme.card(
        ft.Column(
            [
                theme.label("【题目】可以直接修改"),
                stem_field,
                ft.Row(
                    [theme.button(btn_text,
                                  lambda e=None: _switch_question(app),
                                  bgcolor=theme.SLATE)],
                    spacing=8,
                ),
            ],
            spacing=10,
        )
    )


def _rubric_card(app, fields: list) -> ft.Container:
    st = app.state
    q = st.get("question")
    # 正式采分点条数 = 满分 ÷ 每点分值；后面多出来的都是 AI 给的候补点
    need = 0
    if q is not None:
        ps = float(app.cfg["question"].get("point_score", 2)) or 2
        need = max(1, int(round(float(q.max_score) / ps)))
    total = sum(p.score for p in q.points) if q is not None else 0.0

    rows: list = []
    for i, (tf_text, tf_score) in enumerate(fields, start=1):
        def make_delete(idx: int):
            return lambda e=None: _delete_point(app, idx)

        spare = bool(need) and i > need
        label = f"候补{i - need}" if spare else f"第{i}点"
        rows.append(
            ft.Row(
                [
                    ft.Text(label, size=13, width=56,
                            color=theme.MUTED if not spare else theme.BLUE),
                    tf_text,
                    tf_score,
                    _small_btn("删除", make_delete(i - 1), theme.RED),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
        )

    head = "【评分细则】AI 起草，请逐条核对；这是 AI 阅卷的唯一依据"
    notes: list = []
    if q is not None and need and len(fields) > need:
        notes.append(
            f"满分 {q.max_score:g} 分只需 {need} 个采分点，"
            f"后面 {len(fields) - need} 条是【候补】（同一问下另一条也对，"
            f"留着换也行）。学生全答对也只有 {q.max_score:g} 分，多答不加分。"
        )
    if q is not None and total < float(q.max_score) - 1e-6:
        notes.append(
            f"⚠ 采分点合计只有 {total:g} 分，少于满分 {q.max_score:g} 分，"
            f"学生最多只能拿到 {total:g} 分。"
        )

    sum_text = ""
    if q is not None:
        sum_text = f"采分点合计 {total:g} 分 / 满分 {q.max_score:g} 分"

    return theme.card(
        ft.Column(
            [
                theme.label(head),
                *rows,
                ft.Row(
                    [
                        _small_btn("+ 增加一条采分点",
                                   lambda e=None: _add_point(app), theme.GREEN),
                        ft.Text(sum_text, size=12, color=theme.MUTED),
                    ],
                    spacing=12,
                ),
                *[ft.Text(n, size=12, color=theme.MUTED) for n in notes],
            ],
            spacing=10,
        )
    )


def _repeat_card(app) -> ft.Container:
    st = app.state

    def make_select(n: int):
        return lambda e=None: _select_repeats(app, n)

    chips = [
        theme.chip(text, make_select(n), selected=(st["repeats"] == n))
        for n, text in REPEAT_OPTIONS
    ]
    return theme.card(
        ft.Column(
            [
                theme.label("【AI 阅卷的稳定档位】同一份答卷让 AI 独立判几次，取多数结果"),
                ft.Row(chips, spacing=10, wrap=True),
                ft.Text(
                    "次数越多越稳，但耗时和费用也越多。40 人时：1 次约半分钟，"
                    "3 次约 1-2 分钟，5 次约 3 分钟。",
                    size=12, color=theme.MUTED,
                ),
            ],
            spacing=10,
        )
    )


def _select_repeats(app, n: int) -> None:
    app.state["repeats"] = n
    _refresh(app)


def _add_point(app) -> None:
    _sync_fields(app)
    q = app.state["question"]
    q.points.append(
        RubricPoint(
            seq=len(q.points) + 1,
            text="",
            score=float(app.cfg["question"].get("point_score", 2)),
        )
    )
    _refresh(app)


def _delete_point(app, idx: int) -> None:
    _sync_fields(app)
    q = app.state["question"]
    if len(q.points) <= 1:
        return
    if 0 <= idx < len(q.points):
        q.points.pop(idx)
    _refresh(app)


def _sync_fields(app) -> None:
    """把界面上的编辑结果写回题目对象。"""
    st = app.state
    q = st["question"]
    if q is None:
        return
    stem = st.get("_stem_field")
    if stem is not None:
        q.stem = stem.value.strip() or q.stem
    new_points = []
    for i, (tf_text, tf_score) in enumerate(st.get("_point_fields", []), start=1):
        text = (tf_text.value or "").strip()
        if not text:
            continue
        try:
            score = float((tf_score.value or "2").strip())
        except ValueError:
            score = float(app.cfg["question"].get("point_score", 2))
        new_points.append(RubricPoint(seq=i, text=text, score=score))
    if new_points:
        q.points = new_points
        # ⚠ 满分【不】随采分点条数变：8 分的题会多带 1 个候补点（合计 10 分），
        #   但真实高考"多答不加分"，满分仍是 8。
        #   只有题目压根没给满分时才用合计兜底。
        if not q.max_score or float(q.max_score) <= 0:
            q.max_score = sum(p.score for p in new_points)


def _refresh(app) -> None:
    st = app.state
    q = st["question"]
    fields = []
    for p in q.points:
        fields.append(
            (
                ft.TextField(value=p.text, multiline=True, expand=True,
                             text_size=13, min_lines=1, max_lines=4),
                ft.TextField(value=f"{p.score:g}", width=80, text_size=13),
            )
        )
    st["_point_fields"] = fields
    render(app)


# ---------------- 业务逻辑 ----------------

async def _do_question(app) -> None:
    st = app.state
    txt, sub, bar = _progress_widgets()
    app.render([_header(app), _progress_card(txt, sub, bar)])
    txt.value = "AI 正在出题……"
    app.page.update()
    # 出题单独计时：它在教师确认细则之前就跑了，跟后面两步不在同一次连续操作里
    sw = Stopwatch()
    reset_api_calls()
    t0 = time.perf_counter()
    try:
        q = await build_question(app.cfg, app.api_key)
    except Exception as e:  # noqa: BLE001
        log.exception("出题失败")
        app.show_error(f"出题失败：{e}",
                       on_retry=lambda e=None: app.page.run_task(_do_question, app))
        return
    q_secs = time.perf_counter() - t0
    q_calls = get_api_calls() - st.get("_q_calls_base", 0)
    st["_t_question"] = q_secs
    st["_q_calls"] = q_calls
    st["question"] = q
    # ⚠ 清掉题库标记：这是 AI 新出的题，不该走"更新已有题"那条路，
    #   也不该被"换一道题"当成题库题来抽。
    st["_qbank_qid"] = None
    st["_qbank_round"] = None
    st["_stem_field"] = ft.TextField(value=q.stem, multiline=True, expand=True,
                                     text_size=15, min_lines=2, max_lines=6)
    _refresh(app)


def _progress_widgets():
    txt = ft.Text("准备中……", size=16, color=theme.SUB)
    sub = ft.Text("", size=12, color=theme.MUTED)
    bar = ft.ProgressBar(value=0, width=420, color=theme.BLUE, bgcolor=theme.LINE)
    return txt, sub, bar


def _progress_card(txt, sub, bar) -> ft.Container:
    return theme.card(
        ft.Column(
            [txt, bar, sub],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.CARD2,
        padding=40,
    )


async def _do_pipeline(app) -> None:
    st = app.state
    _sync_fields(app)
    q = st["question"]
    if not q or not q.points:
        app.show_error("评分细则不能为空，至少保留一条采分点。")
        return

    txt, sub, bar = _progress_widgets()
    app.render([_header(app), _progress_card(txt, sub, bar)])

    def on_answer_progress(done, total):
        txt.value = f"第一步 / 共两步：正在生成学生答卷 {done} / {total}"
        bar.value = done / total * 0.5
        app.page.update()

    def on_grade_progress(done, total):
        txt.value = f"第二步 / 共两步：AI 正在阅卷 {done} / {total}"
        bar.value = 0.5 + done / total * 0.5
        app.page.update()

    # 计时：出题那步在另一个协程里，这里只计"生成 + 阅卷 + 存档"，
    # 三步时间最后合并（出题单独取 st["_t_question"]）。
    sw = Stopwatch()
    calls_base = get_api_calls()

    try:
        sub.value = "生成答卷中，请稍候……"
        app.page.update()
        students = await build_class(app.cfg, app.api_key, q, on_answer_progress)
        sw.mark("answer")
        if not students:
            app.show_error("一份答卷都没生成成功，请检查网络或密钥后重试。",
                           on_retry=lambda e=None: app.page.run_task(_do_pipeline, app))
            return

        sub.value = f"答卷生成完毕（{len(students)} 份），AI 阅卷中……"
        app.page.update()
        ai = await run_grading(app.cfg, app.api_key, q, students,
                               on_grade_progress, repeats=st["repeats"])
        sw.mark("grade")

        txt.value = "正在存档……"
        app.page.update()
        info = sw.as_dict()
        # 出题是之前单独跑的，合并进来；总额 = 出题 + 本次两步 + 存档
        t_q = st.get("_t_question")
        if t_q is not None:
            info["question"] = t_q
            info["total"] = info["total"] + t_q
        api_calls = (st.get("_q_calls") or 0) + (get_api_calls() - calls_base)
        paper_id = persist(q, students, ai, st["repeats"],
                           timing=info, api_calls=api_calls)
        # 题库来的题：把这次练习和存档号对上（报告/历史里能溯源）
        if st.get("_qbank_qid"):
            qbank.link_paper(st["_qbank_qid"], st.get("_qbank_round") or 1,
                             paper_id)
        log.info("本次批改耗时：%s", timing_summary_line(info, api_calls,
                                                       len(students)))
        st["timing"] = info
        st["api_calls"] = api_calls

        st["students"] = students
        st["ai"] = ai
        st["teacher"] = {}
        st["idx"] = 0
        st["paper_id"] = paper_id
        app.go_grading()
    except Exception as e:  # noqa: BLE001
        log.exception("流程失败")
        app.show_error(f"运行失败：{e}",
                       on_retry=lambda e=None: app.page.run_task(_do_pipeline, app))


# ---------------- 页面入口 ----------------

def render(app) -> None:
    st = app.state
    if not app.api_key:
        app.render([_header(app), _no_key_card(app)])
        return

    if st["question"] is None:
        app.render([_header(app), _idle_card(app)])
        return

    q = st["question"]

    if st.get("_stem_field") is None:
        st["_stem_field"] = ft.TextField(
            value=q.stem, multiline=True, expand=True,
            text_size=15, min_lines=2, max_lines=6,
        )
    if "_point_fields" not in st or len(st["_point_fields"]) != len(q.points):
        _refresh(app)
        return
    controls = [
        _header(app),
        _material_card(app),
        _note_card(app),
        _question_card(app, st["_stem_field"]),
        _rubric_card(app, st["_point_fields"]),
        _repeat_card(app),
        theme.card(
            ft.Column(
                [
                    ft.Text(
                        f"确认无误后，将生成 {app.cfg['classroom'].get('students', 40)} "
                        f"份学生答卷，并由 AI 逐份给出基准分与理由。",
                        size=13, color=theme.SUB,
                    ),
                    ft.Row(
                        [theme.button("确认细则，开始生成答卷并阅卷",
                                      lambda e=None: app.page.run_task(_do_pipeline, app))],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                ],
                spacing=12,
            ),
            bgcolor=theme.CARD2,
        ),
    ]
    app.render(controls)
