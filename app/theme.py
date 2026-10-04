"""界面配色与通用组件。改这里就能换整个软件的外观。"""

import flet as ft

# ---- 配色 ----
BG = "#E8E2D4"        # 页面底色
CARD = "#FBF8F0"      # 卡片
CARD2 = "#F2EEDF"     # 次级卡片
PAPER = "#FBFAF5"     # 答卷纸
BORDER = "#E2DBC8"
LINE = "#D5CCB6"

TEXT = "#2A2A35"
SUB = "#5A5346"
MUTED = "#8A8272"
INK = "#1A237E"       # 手写墨色

BLUE = "#3D7EFF"
GREEN = "#3FA34D"
AMBER = "#E8A33D"
RED = "#D62828"
SLATE = "#5A7FBF"


def card(content, bgcolor: str = CARD, padding: int = 16, radius: int = 10,
         border: str = BORDER, **kwargs) -> ft.Container:
    return ft.Container(
        content=content,
        bgcolor=bgcolor,
        border_radius=radius,
        padding=padding,
        border=ft.Border.all(1, border),
        **kwargs,
    )


def button(text: str, on_click, bgcolor: str = BLUE, size: int = 15,
           width=None, height=None) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=size, color="#FFFFFF"),
        bgcolor=bgcolor,
        border_radius=10,
        padding=ft.Padding.symmetric(horizontal=24, vertical=12),
        alignment=ft.Alignment.CENTER,
        width=width,
        height=height,
        on_click=on_click,
        ink=True,
    )


def chip(text: str, on_click, selected: bool = False) -> ft.Container:
    return ft.Container(
        content=ft.Text(
            text, size=13,
            color="#FFFFFF" if selected else SUB,
        ),
        bgcolor=BLUE if selected else CARD2,
        border_radius=8,
        border=ft.Border.all(1, BLUE if selected else LINE),
        padding=ft.Padding.symmetric(horizontal=14, vertical=8),
        on_click=on_click,
        ink=True,
    )


def label(text: str, size: int = 12, color: str = MUTED) -> ft.Text:
    return ft.Text(text, size=size, color=color)


def title(text: str, size: int = 15, color: str = SUB) -> ft.Text:
    return ft.Text(text, size=size, color=color)


def stat_block(caption: str, value: str, color: str) -> ft.Column:
    return ft.Column(
        [
            ft.Text(caption, size=11, color=MUTED),
            ft.Text(value, size=20, color=color, weight=ft.FontWeight.BOLD),
        ],
        spacing=2,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )


def diff_color(diff: float) -> str:
    if abs(diff) < 1e-6:
        return GREEN
    if abs(diff) <= 1:
        return AMBER
    return RED
