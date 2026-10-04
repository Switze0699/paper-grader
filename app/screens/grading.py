"""第二步：教师盲评。这一页看不到 AI 的任何判定。"""

from __future__ import annotations

import flet as ft

from app import theme
from storage import repository as repo


def _rubric_small(app) -> ft.Container:
    q = app.state["question"]
    return theme.card(
        ft.Column(
            [
                theme.label("【评分细则】"),
                ft.Text(q.rubric_text(), size=13, color="#3A3A4A"),
            ],
            spacing=8,
        ),
        bgcolor=theme.CARD2,
        padding=14,
    )


def _question_small(app) -> ft.Container:
    """盲评页的题面卡。

    ⚠ 2026-10-04：用 full_stem()，题库来的题必须显示材料。
       教师看不到材料就没法判断学生有没有点出材料里的条件。
       （AI 随机出题的老题 material 为空，显示与从前完全一样。）
    """
    q = app.state["question"]
    return theme.card(
        ft.Column(
            [theme.label("【题目】"),
             ft.Text(q.full_stem(), size=14, color=theme.TEXT)],
            spacing=8,
        ),
        padding=14,
    )


def _answer_card(app, student) -> ft.Container:
    size = int(app.cfg["ui"].get("answer_font_size", 18))
    return theme.card(
        ft.Column(
            [
                theme.label(f"【第 {app.state['idx'] + 1} 份答卷】"),
                ft.Container(
                    content=ft.Text(
                        student.answer or "（该生未作答）",
                        size=size,
                        color=theme.INK,
                        font_family="Handwrite",
                        selectable=True,
                    ),
                    bgcolor=theme.PAPER,
                    border=ft.Border.all(1, theme.LINE),
                    border_radius=6,
                    padding=16,
                    width=999999,
                ),
            ],
            spacing=10,
        )
    )


def _do_grade(app, value: int) -> None:
    st = app.state
    student = st["students"][st["idx"]]
    st["teacher"][student.seq] = value
    if st["paper_id"]:
        repo.save_teacher_grade(st["paper_id"], student.seq, value)
    render(app)


def _next(app) -> None:
    st = app.state
    st["idx"] += 1
    if st["idx"] >= len(st["students"]):
        if st["paper_id"]:
            repo.set_paper_status(st["paper_id"], "done")
        app.go_report()
    else:
        render(app)


def _prev(app) -> None:
    st = app.state
    if st["idx"] > 0:
        st["idx"] -= 1
        render(app)


def render(app) -> None:
    st = app.state
    students = st["students"]
    if not students:
        app.go_setup()
        return

    idx = st["idx"]
    if idx >= len(students):
        app.go_report()
        return

    q = st["question"]
    student = students[idx]
    given = st["teacher"].get(student.seq)

    max_score = int(round(q.max_score))
    buttons = []
    for v in range(0, max_score + 1):
        selected = (given == v)
        buttons.append(
            ft.Container(
                content=ft.Text(str(v), size=17, color="#FFFFFF",
                                weight=ft.FontWeight.BOLD),
                bgcolor=theme.BLUE if selected else theme.SLATE,
                border_radius=10,
                width=52,
                height=52,
                alignment=ft.Alignment.CENTER,
                border=ft.Border.all(2, theme.BLUE if selected else theme.SLATE),
                on_click=lambda e=None, vv=v: _do_grade(app, vv),
                ink=True,
            )
        )

    next_row = []
    if idx > 0:
        next_row.append(
            theme.button("← 上一份", lambda e=None: _prev(app), bgcolor=theme.SLATE)
        )
    if given is not None:
        last = idx == len(students) - 1
        next_row.append(
            theme.button(
                "查看对比报告 →" if last else "下一份 →",
                lambda e=None: _next(app),
            )
        )

    controls = [
        ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        f"{q.subject} · {q.topic} · 满分 {q.max_score:g}",
                        size=15, color=theme.SUB,
                    ),
                    ft.Text(
                        f"进度：第 {idx + 1} / {len(students)} 份"
                        + (f"　已给分：{given}" if given is not None else ""),
                        size=13, color=theme.MUTED,
                    ),
                    ft.Divider(height=1, color=theme.BORDER),
                ],
                spacing=6,
            ),
            bgcolor=theme.CARD2,
            padding=16,
            border_radius=10,
            border=ft.Border.all(1, theme.BORDER),
        ),
        _question_small(app),
        _rubric_small(app),
        _answer_card(app, student),
        theme.card(
            ft.Column(
                [
                    ft.Text(f"请给这份答卷打分（满分 {q.max_score:g}）",
                            size=13, color=theme.SUB),
                    ft.Row(buttons, spacing=8, wrap=True,
                           alignment=ft.MainAxisAlignment.CENTER),
                    ft.Row(next_row, spacing=12,
                           alignment=ft.MainAxisAlignment.CENTER),
                    ft.Text("AI 的判定已封存，改完后才能看到，请凭自己的经验打分。",
                            size=12, color=theme.MUTED),
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=theme.CARD2,
            padding=20,
        ),
    ]
    app.render(controls)
