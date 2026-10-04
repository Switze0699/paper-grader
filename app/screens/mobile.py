"""「手机访问」页面：告诉用户怎么用手机打开电脑上的这个程序。

用最保守的 Flet 写法（只用项目里已经在用的那些 API），
免得因为 Flet 版本差异导致启动就崩——这个页面崩了整个软件就打不开了。
"""

from __future__ import annotations

import flet as ft

from app import theme
from core import lan


def _qr_block(url: str) -> list:
    """二维码那一块；生成不出来就返回空（下面还有文字网址兜底）。"""
    qr = lan.qr_data_url(url)
    if not qr:
        return []
    return [
        theme.card(
            ft.Column(
                [
                    ft.Image(src=qr, width=200, height=200),
                    ft.Text("用手机相机扫这个码", size=13, color=theme.SUB),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor="#FFFFFF",
        ),
    ]


def _step(n: int, text: str) -> ft.Container:
    return ft.Row(
        [
            ft.Container(
                content=ft.Text(str(n), size=12, color="#FFFFFF"),
                bgcolor=theme.BLUE,
                border_radius=9,
                width=20,
                height=20,
                alignment=ft.Alignment.CENTER,
            ),
            ft.Text(text, size=13, color=theme.TEXT),
        ],
        spacing=8,
    )


def render(app) -> None:
    ip = lan.lan_ip()
    url = lan.access_url(8550)

    body = [
        ft.Text("手机访问", size=22, color=theme.TEXT),
        theme.card(
            ft.Text(
                "电脑保持开机，手机连同一个 WiFi，用手机浏览器打开下面这个网址就行。",
                size=14, color=theme.SUB,
            )
        ),
        theme.card(
            ft.Column(
                [
                    ft.Text("在手机浏览器里打开", size=12, color=theme.SUB),
                    ft.Text(url, size=20, color=theme.BLUE),
                    ft.Text(f"（本机局域网 IP：{ip}）", size=12, color=theme.MUTED),
                ],
                spacing=4,
            ),
            bgcolor=theme.CARD2,
        ),
    ]
    body += _qr_block(url)
    body += [
        theme.card(
            ft.Column(
                [
                    ft.Text("打不开时按这个顺序试", size=15, color=theme.TEXT),
                    _step(1, "确认手机和电脑连的是同一个 WiFi"),
                    _step(2, "Windows 弹出「允许 Python 访问网络」时点「允许」"),
                    _step(3, "在手机浏览器地址栏手动输入上面那个网址"),
                    _step(4, "学校/公司 WiFi 常开了设备隔离，改用手机热点让电脑连"),
                ],
                spacing=8,
            )
        ),
        theme.card(
            ft.Text(
                "说明：手机和电脑共用同一个程序。你在电脑上出的题、批的分，"
                "手机上刷新一下就能看到，反之也一样。"
                "所以别关掉启动它的那个黑窗口，关了手机就连不上了。",
                size=13, color=theme.SUB,
            ),
            bgcolor="#FFF6DF",
        ),
        theme.button("返回", lambda e=None: app.go_setup(), bgcolor=theme.SLATE),
    ]
    app.render(body)
