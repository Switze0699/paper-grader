"""回溯单份：看 AI 是怎么判的，逐点给出理由和踩点证据。"""

from __future__ import annotations

import flet as ft

from app import theme

LEVEL_TEXT = {2: "完全命中", 1: "部分命中", 0: "未命中"}
LEVEL_COLOR = {2: theme.GREEN, 1: theme.AMBER, 0: theme.RED}


def _point_row(pr) -> ft.Container:
    return theme.card(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(f"第 {pr.point_seq} 点", size=13, color=theme.MUTED,
                                width=64),
                        ft.Container(
                            content=ft.Text(
                                f"{LEVEL_TEXT.get(pr.level, '—')} "
                                f"({pr.score * pr.level / 2:g} / {pr.score:g} 分)",
                                size=12, color="#FFFFFF",
                            ),
                            bgcolor=LEVEL_COLOR.get(pr.level, theme.MUTED),
                            border_radius=6,
                            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                        ),
                        ft.Text(f"一致率 {pr.agreement * 100:.0f}%", size=11,
                                color=theme.MUTED),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Text(pr.text, size=13, color=theme.TEXT),
                ft.Text(f"AI 理由：{pr.reason or '（无）'}", size=12, color=theme.SUB),
                ft.Text(
                    f"踩点证据：{pr.evidence or '（答案中未找到对应内容）'}",
                    size=12, color=theme.INK,
                ),
            ],
            spacing=6,
        ),
        bgcolor=theme.CARD2,
        padding=12,
    )


def render(app) -> None:
    st = app.state
    seq = st.get("view_seq")
    student = next((s for s in st["students"] if s.seq == seq), None)
    if student is None:
        app.go_report()
        return

    a = st["ai"].get(seq)
    t = st["teacher"].get(seq)
    q = st["question"]

    diff = (t - a.total) if (t is not None and a is not None) else None
    diff_color = theme.diff_color(diff) if diff is not None else theme.MUTED

    size = int(app.cfg["ui"].get("answer_font_size", 18))
    controls = [
        ft.Row(
            [
                theme.button("← 返回报告", lambda e=None: app.go_report(),
                             bgcolor=theme.SLATE),
            ],
            alignment=ft.MainAxisAlignment.START,
        ),
        theme.card(
            ft.Row(
                [
                    theme.stat_block("你的给分", str(t) if t is not None else "—",
                                     theme.BLUE),
                    theme.stat_block("AI 给分", f"{a.total:g}" if a else "—",
                                     theme.TEXT),
                    theme.stat_block(
                        "差值", f"{diff:+g}" if diff is not None else "—", diff_color
                    ),
                    theme.stat_block(
                        "AI 置信度",
                        f"{a.confidence * 100:.0f}%" if a else "—",
                        theme.RED if (a and a.low_confidence) else theme.GREEN,
                    ),
                ],
                spacing=34,
                wrap=True,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            bgcolor=theme.CARD2,
            padding=20,
        ),
        theme.card(
            ft.Column(
                [
                    theme.label(f"【第 {seq} 份答卷】"),
                    ft.Container(
                        content=ft.Text(
                            student.answer or "（该生未作答）",
                            size=size, color=theme.INK, font_family="Handwrite",
                            selectable=True,
                        ),
                        bgcolor=theme.PAPER,
                        border=ft.Border.all(1, theme.LINE),
                        border_radius=6,
                        padding=16,
                    ),
                ],
                spacing=10,
            )
        ),
        theme.card(
            ft.Column(
                [theme.label("【评分细则】"),
                 ft.Text(q.rubric_text(), size=13, color="#3A3A4A")],
                spacing=8,
            ),
            bgcolor=theme.CARD2,
            padding=14,
        ),
    ]

    if a:
        controls.append(
            theme.card(
                ft.Column(
                    [
                        theme.label("【AI 逐点判定】"),
                        *[_point_row(pr) for pr in a.points],
                    ],
                    spacing=10,
                )
            )
        )
        if a.comment:
            controls.append(
                theme.card(
                    ft.Column(
                        [theme.label("【AI 总评】"),
                         ft.Text(a.comment, size=13, color=theme.TEXT)],
                        spacing=8,
                    ),
                    bgcolor=theme.CARD2,
                )
            )
        controls.append(
            ft.Container(
                content=ft.Text(
                    f"AI 对这份答卷独立判定了 {st['repeats']} 次，"
                    f"总分最大波动 {a.score_range:g} 分。",
                    size=12, color=theme.MUTED,
                ),
                padding=ft.Padding.only(left=4),
            )
        )

    app.render(controls)
