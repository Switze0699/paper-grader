"""历史记录：回看以前批过的卷子，或接着没批完的继续。"""

from __future__ import annotations

import flet as ft

from app import theme
from storage import repository as repo


def _open(app, paper_id: int) -> None:
    paper = repo.load_paper(paper_id)
    if not paper:
        app.show_error("找不到这份记录。")
        return
    st = app.state
    st["question"] = paper["question"]
    st["students"] = paper["students"]
    st["ai"] = paper["ai"]
    st["teacher"] = paper["teacher"]
    st["paper_id"] = paper["paper_id"]
    st["repeats"] = paper["repeats"]
    st["_stem_field"] = None
    st.pop("_point_fields", None)

    # 定位到第一份还没判过的答卷
    idx = 0
    for i, s in enumerate(paper["students"]):
        if s.seq not in paper["teacher"]:
            idx = i
            break
    else:
        if paper["teacher"]:
            idx = len(paper["students"])
    st["idx"] = idx

    if idx >= len(paper["students"]):
        app.go_report()
    else:
        app.go_grading()


def _row(app, p: dict) -> ft.Container:
    stem = (p.get("stem") or "").strip().replace("\n", " ")
    if len(stem) > 34:
        stem = stem[:34] + "…"
    total = p.get("s_cnt", 0)
    done = p.get("t_cnt", 0)
    status = "已批改" if (total and done >= total) else (
        f"已批 {done}/{total}" if done else "未批改"
    )
    color = theme.GREEN if status == "已批改" else theme.AMBER

    return ft.Container(
        content=ft.Row(
            [
                ft.Column(
                    [
                        ft.Text(f"第 {p['id']} 份 · {p.get('topic') or '综合题'}",
                                size=14, color=theme.TEXT),
                        ft.Text(f"{p.get('created_at', '')}　{stem}",
                                size=12, color=theme.MUTED),
                    ],
                    spacing=2,
                    expand=True,
                ),
                ft.Container(
                    content=ft.Text(status, size=12, color="#FFFFFF"),
                    bgcolor=color,
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                ),
                ft.Text(f"{p.get('max_score', 0):g} 分", size=12, color=theme.MUTED,
                        width=52),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.CARD,
        border=ft.Border.all(1, theme.BORDER),
        border_radius=10,
        padding=14,
        on_click=lambda e=None: _open(app, int(p["id"])),
        ink=True,
    )


def render(app) -> None:
    papers = repo.list_papers()
    if not papers:
        body = [
            theme.card(ft.Text("还没有任何批改记录。", size=15, color=theme.SUB)),
            theme.button("回到首页", lambda e=None: app.go_setup()),
        ]
        app.render(body)
        return

    controls = [
        ft.Container(
            content=ft.Column(
                [
                    ft.Text("历史记录", size=20, color=theme.TEXT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("点任意一条可以回看报告，或接着没批完的部分继续。",
                            size=13, color=theme.MUTED),
                ],
                spacing=6,
            ),
            bgcolor=theme.CARD2,
            padding=16,
            border_radius=10,
            border=ft.Border.all(1, theme.BORDER),
        ),
        *[_row(app, p) for p in papers],
        theme.button("回到首页", lambda e=None: app.go_setup(), bgcolor=theme.SLATE),
    ]
    app.render(controls)
