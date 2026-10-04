"""第三步：教师分与 AI 分的对比报告。"""

from __future__ import annotations

import flet as ft

from app import theme
from core.report import compare, verdict_text
from core.timing import STEP_LABELS, fmt_duration
from storage import repository as repo
from storage.export import export_excel


def _timing_card(app) -> ft.Container | None:
    """本次批改的耗时统计（2026-10-04 新增）。

    数据存在 papers 表的 t_question / t_answer / t_grade / t_total 四列。
    老存档没有这些数据（NULL），那就整块不显示 —— 免得给用户看一片"—"。
    """
    st = app.state
    # ⚠ 键名要统一：界面跑完时 st["timing"] 的键是 question/answer/grade/total
    #   （Stopwatch.as_dict() 产出的），数据库读回来的也是同一套。
    #   只有 papers 表的列名带 t_ 前缀，这里读出来就已经转成不带前缀的了。
    info = st.get("timing")
    calls = st.get("api_calls")
    # 界面上刚跑完就有 st["timing"]；从历史记录点进来的话去数据库读
    if not info:
        pid = st.get("paper_id")
        if pid:
            row = repo.load_timing(pid)
            if row and row.get("t_total") is not None:
                info = {
                    "question": row.get("t_question"),
                    "answer": row.get("t_answer"),
                    "grade": row.get("t_grade"),
                    "total": row.get("t_total"),
                }
                calls = calls or row.get("api_calls")
    if not info or info.get("total") is None:
        return None

    def _row(key: str, value, hint: str = "") -> ft.Column:
        return ft.Column(
            [
                ft.Text(STEP_LABELS.get(key, key), size=12, color=theme.MUTED),
                ft.Text(fmt_duration(value), size=17, color=theme.TEXT,
                        weight=ft.FontWeight.BOLD),
                ft.Text(hint, size=11, color=theme.MUTED),
            ],
            spacing=2,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

    n = len(st.get("students") or [])
    blocks = [
        _row("question", info.get("question"), "AI 出题"),
        _row("answer", info.get("answer"),
             f"生成 {n} 份答卷" if n else "生成答卷"),
        _row("grade", info.get("grade"),
             f"AI 阅卷 {n} 份" if n else "AI 阅卷"),
        _row("total", info.get("total"),
             f"用了 {calls} 次 API" if calls else ""),
    ]
    return theme.card(
        ft.Column(
            [
                theme.label("【本次批改耗时】"),
                ft.Row(blocks, spacing=36, wrap=True,
                       alignment=ft.MainAxisAlignment.CENTER),
            ],
            spacing=12,
        ),
        bgcolor=theme.CARD2,
    )


def _stat_row(stats: dict, verdict: str, color: str) -> ft.Container:
    return theme.card(
        ft.Column(
            [
                theme.label("【你的批改 vs AI 考官】"),
                ft.Row(
                    [
                        theme.stat_block("你的平均分", f"{stats['avg_teacher']:.2f}",
                                         theme.BLUE),
                        theme.stat_block("AI 平均分", f"{stats['avg_ai']:.2f}",
                                         theme.TEXT),
                        theme.stat_block("偏差", f"{stats['bias']:+.2f}", color),
                    ],
                    spacing=40,
                    wrap=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=ft.Text(verdict, size=14, color="#FFFFFF",
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=color,
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=14, vertical=6),
                    margin=ft.Margin.only(top=8),
                ),
            ],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.CARD2,
        padding=24,
    )


def _detail_row(stats: dict) -> ft.Container:
    items = [
        ("完全一致", f"{stats['exact_rate'] * 100:.0f}%"),
        ("相差 ≤1 分", f"{stats['within1_rate'] * 100:.0f}%"),
        ("平均绝对误差", f"{stats['mae']:.2f}"),
        ("最大单份差距", f"{stats['max_abs_diff']:.0f}"),
        ("你偏松的份数", str(stats["loose_count"])),
        ("你偏紧的份数", str(stats["strict_count"])),
    ]
    corr = stats.get("correlation")
    if corr is not None:
        items.append(("相关性", f"{corr:.2f}"))
    blocks = [
        ft.Column(
            [
                ft.Text(k, size=11, color=theme.MUTED),
                ft.Text(v, size=15, color=theme.TEXT, weight=ft.FontWeight.BOLD),
            ],
            spacing=2,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        for k, v in items
    ]
    return theme.card(
        ft.Column(
            [theme.label("【细致指标】"), ft.Row(blocks, spacing=26, wrap=True,
                                                alignment=ft.MainAxisAlignment.CENTER)],
            spacing=10,
        )
    )


def _cells(app, stats: dict) -> ft.Container:
    st = app.state
    cells = []
    for row in stats["rows"]:
        seq = row["seq"]
        m, a = row["teacher"], row["ai"]
        d = row["diff"]
        color = theme.diff_color(d)
        if abs(d) < 1e-6:
            bg = "#F0F5E8"
        elif abs(d) <= 1:
            bg = "#FDF6E3"
        else:
            bg = "#FBEFEF"
        mark = " ?" if row.get("low_confidence") else ""
        cells.append(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Text(f"{seq:02d}{mark}", size=10, color=theme.MUTED),
                        ft.Text(f"{m:g} / {a:g}", size=13, color=color,
                                weight=ft.FontWeight.BOLD),
                    ],
                    spacing=1,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=bg,
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=6, vertical=6),
                width=68,
                on_click=lambda e=None, s=seq: app.go_review(s),
                ink=True,
            )
        )
    return theme.card(
        ft.Column(
            [
                theme.label("【逐份对照 · 你的分 / AI 分（点击查看 AI 的判定依据）】"),
                ft.Row(cells, wrap=True, spacing=6, run_spacing=6),
                ft.Text("带 ? 的表示 AI 自己判得不够一致，建议你重点复核。",
                        size=12, color=theme.MUTED),
            ],
            spacing=12,
        )
    )


def _export(app, stats: dict) -> None:
    from core.paths import _is_android
    from storage.repository import load_paper

    paper = load_paper(app.state["paper_id"])
    path = export_excel(paper)
    if path is None:
        app.page.add(ft.Text("导出失败", size=12, color=theme.RED))
        return

    # 安卓上文件存在应用私有目录里，用户在文件管理器里根本找不到。
    # 这时交给系统的"分享"，用户可以发到微信/网盘/邮件，才拿得到文件。
    if _is_android():
        try:
            ft.Share(
                files=[
                    ft.ShareFile(
                        path=str(path),
                        name=path.name,
                        mime_type=("application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet"),
                    )
                ],
                text=f"第{paper.get('paper_id')}份的批改记录",
            )
            app.page.add(ft.Text("已调起系统分享，选个方式发出去即可保存",
                                 size=12, color=theme.GREEN))
            return
        except Exception as e:      # 分享失败也别让程序崩
            app.page.add(ft.Text(
                f"分享失败（{e}）。文件已存在：{path}", size=12, color=theme.RED))
            return

    app.page.add(ft.Text(f"已导出：{path}", size=12, color=theme.GREEN))


def render(app) -> None:
    st = app.state
    stats = compare(st["students"], st["teacher"], st["ai"])
    if stats["n"] == 0:
        app.render([
            theme.card(ft.Text("还没有批改记录。", size=15, color=theme.SUB)),
            theme.button("回到首页", lambda e=None: app.go_setup()),
        ])
        return

    verdict, color = verdict_text(stats)

    controls = [
        ft.Container(
            content=ft.Column(
                [
                    ft.Text("批改报告", size=20, color=theme.TEXT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text(
                        f"共 {stats['n']} 份 · AI 每份独立判定 "
                        f"{st['repeats']} 次取多数"
                        + (f" · 其中 {stats['low_confidence_count']} 份 AI 判得不够一致"
                           if stats["low_confidence_count"] else ""),
                        size=13, color=theme.MUTED,
                    ),
                ],
                spacing=6,
            ),
            bgcolor=theme.CARD2,
            padding=16,
            border_radius=10,
            border=ft.Border.all(1, theme.BORDER),
        ),
        _stat_row(stats, verdict, color),
        _timing_card(app),
        _detail_row(stats),
        _cells(app, stats),
        theme.card(
            ft.Row(
                [
                    theme.button("导出 Excel", lambda e=None: _export(app, stats),
                                 bgcolor=theme.GREEN),
                    theme.button("历史记录", lambda e=None: app.go_history(),
                                 bgcolor=theme.SLATE),
                    theme.button("出一份新卷", lambda e=None: app.go_setup(),
                                 bgcolor=theme.BLUE),
                ],
                spacing=12,
                wrap=True,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            bgcolor=theme.CARD2,
        ),
    ]
    app.render(controls)
