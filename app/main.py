"""程序入口：双击「start_desktop.bat」就是运行这个文件。"""

from __future__ import annotations

import logging
import os
import sys

import flet as ft

from app import theme
from app.screens import grading as screen_grading
from app.screens import history as screen_history
from app.screens import mobile as screen_mobile
from app.screens import report as screen_report
from app.screens import review as screen_review
from app.screens import setup as screen_setup
from core import paths
from core.config import get_api_key, load_config, setup_logging

log = logging.getLogger(__name__)


class App:
    """整个软件的状态机：负责在几个页面之间切换。"""

    def __init__(self, page: ft.Page):
        self.page = page
        self.cfg = load_config()
        self.api_key = get_api_key(self.cfg)

        self.state = {
            "question": None,
            "students": [],
            "ai": {},
            "teacher": {},
            "idx": 0,
            "view_seq": None,
            "paper_id": None,
            "repeats": int(self.cfg["grading"].get("repeats", 3)),
        }

        self.body = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=14)
        self._init_page()
        page.add(
            ft.Container(
                content=self.body, bgcolor=theme.BG, padding=16, expand=True
            )
        )
        self.go_setup()

    # ---------- 基础 ----------

    def _init_page(self) -> None:
        p = self.page
        p.title = "批改模拟器 · AI 虚拟考官"
        p.bgcolor = theme.BG
        p.padding = 0
        font = self.cfg["ui"].get("answer_font", "")
        if font:
            p.fonts = {"Handwrite": font}
        try:
            p.window.width = int(self.cfg["ui"].get("window_width", 1080))
            p.window.height = int(self.cfg["ui"].get("window_height", 880))
        except Exception:  # noqa: BLE001
            pass

    def render(self, controls: list) -> None:
        self.body.controls = controls
        self.page.update()

    def show_error(self, message: str, on_retry=None) -> None:
        """出错时统一显示一张说明卡片。"""
        children = [
            ft.Text("出错了", size=18, color=theme.RED, weight=ft.FontWeight.BOLD),
            ft.Text(message, size=14, color=theme.TEXT),
            ft.Text(
                "详细信息可在 logs 文件夹的 app.log 里查看", size=12, color=theme.MUTED
            ),
        ]
        if on_retry:
            children.append(theme.button("重试", on_retry))
        children.append(theme.button("回到首页", lambda e=None: self.go_setup(),
                                     bgcolor=theme.SLATE))
        self.render([theme.card(ft.Column(children, spacing=12), bgcolor=theme.CARD2)])

    # ---------- 页面切换 ----------

    def go_setup(self) -> None:
        screen_setup.render(self)

    def go_grading(self) -> None:
        screen_grading.render(self)

    def go_report(self) -> None:
        screen_report.render(self)

    def go_review(self, seq: int) -> None:
        self.state["view_seq"] = seq
        screen_review.render(self)

    def go_history(self) -> None:
        screen_history.render(self)

    def go_mobile(self) -> None:
        screen_mobile.render(self)


async def main(page: ft.Page):
    setup_logging()
    try:
        App(page)
    except Exception as e:  # noqa: BLE001
        log.exception("程序启动失败")
        page.add(ft.Text(f"启动失败：{e}", size=15, color="#B02A2A"))


def _fix_console_streams() -> None:
    """无控制台的 exe 里 sys.stdout/stderr 是 None，print 和日志会崩，这里补成空输出。"""
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull


if __name__ == "__main__":
    _fix_console_streams()
    setup_logging()
    ft.run(main, assets_dir=str(paths.resource("assets")))
