"""局域网访问的小工具：查本机 IP、把网址做成二维码。

【背景】"手机访问"模式：程序在电脑上跑成一个网页服务，
手机连同一个 WiFi，用浏览器打开电脑的地址就能用。
二维码是为了让用户不用手打 192.168.x.x 这种地址。
"""

from __future__ import annotations

import base64
import io
import logging
import socket

log = logging.getLogger(__name__)

# 连过去但不会真的发包，只是为了让系统告诉我们"出口 IP 是多少"
_PROBE = ("223.5.5.5", 80)


def lan_ip() -> str:
    """取本机在局域网里的 IP——手机要访问的就是这个。

    为什么不用 socket.gethostbyname(gethostname())：
    那个函数在很多 Windows 机器上会返回 127.0.1.1（回环地址），
    手机照着它访问会连到自己。探针法拿到的才是真正的局域网地址。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.3)
        s.connect(_PROBE)
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def access_url(port: int = 8550) -> str:
    """手机要访问的完整网址。"""
    return f"http://{lan_ip()}:{port}"


def qr_data_url(text: str) -> str:
    """把一段文字做成二维码，返回能直接喂给 ft.Image 的 data URL。

    失败时返回空字符串（调用方显示文字网址兜底即可，不影响使用）。
    """
    try:
        import segno
    except Exception:  # noqa: BLE001
        log.warning("没装 segno，二维码不可用（不影响手机访问）")
        return ""
    try:
        buf = io.BytesIO()
        segno.make(text, error="m").save(
            buf, kind="png", scale=5, border=2, dark="#1A2230"
        )
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("生成二维码失败：%s", e)
        return ""
