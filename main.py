"""Flet 应用入口。

⚠ `flet build apk` 打包时找的是**项目根目录的这个文件**，
   所以真正的界面代码在 app/main.py，这里只做转发。

   桌面 exe 的入口仍然是 app/main.py（PyInstaller 从那里打），
   两边共用同一套 app/ 代码，不会分叉。
"""

import flet as ft

from app.main import main
from core import paths

if __name__ == "__main__":
    ft.run(main, assets_dir=str(paths.resource("assets")))
