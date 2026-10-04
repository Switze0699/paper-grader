# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把批改模拟器打包成一个无黑框的 exe。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe -m PyInstaller build_exe.spec --noconfirm
"""

import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)

# 项目资源：一起打进 exe 里
#
# ⚠ assets/index.html 不要打进包：那是网页版启动时**自动生成**的加载进度页
#   （见 app/server.py 的 install_progress_page），每次启动都会重装。
#   打进包既没用，还可能和运行时生成的那份冲突。
datas = [
    (os.path.join(ROOT, "prompts"), "prompts"),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "config.yaml"), "."),
    (os.path.join(ROOT, ".env.example"), "."),
]

binaries = []

# assets 里的 index.html 是网页版运行时生成的加载进度页，不该打进包。
# PyInstaller 的 datas 只支持 (src, dst) 形式，所以这里把 assets 展开成
# 逐个文件（排除 index.html），保持"其余资源照旧打进 exe"。
_asset_src = os.path.join(ROOT, "assets")
if os.path.isdir(_asset_src):
    for _name in sorted(os.listdir(_asset_src)):
        if _name == "index.html":
            continue          # 运行时生成，见 app/server.py
        _p = os.path.join(_asset_src, _name)
        if os.path.isdir(_p):
            datas.append((_p, os.path.join("assets", _name)))
        else:
            datas.append((_p, "assets"))
    # 把原来那条整个 assets 的记录去掉（现在按文件逐个收，更精确）
    datas = [d for d in datas if d[0] != _asset_src]

hiddenimports = [
    # Flet 桌面模式会在运行时动态导入这两个包，PyInstaller 扫不到，必须手动加
    "flet_desktop",
    "flet_web",
    "flet_web.fastapi",
    "flet_web.fastapi.serve_fastapi_web_app",
    "flet_web.fastapi.flet_static_files",
    "uvicorn",
    "uvicorn.loops.auto",
    "uvicorn.protocols.websockets.auto",
    "websockets",
    "httpx",
    "openpyxl",
    "yaml",
    "dotenv",
    # ⚠ 题库（2026-10-04/05）：exe 的入口是 main.py，它不import
    #   import_questions.py，所以 PyInstaller 静态分析扫不到
    #   core.qbank_parse（只有导入器在用）。这里手动补上，
    #   免得哪天想在 exe 里做导入时才发现模块不存在。
    #   core.qbank 本身会被 app/screens/setup.py import，已经自动带上。
    "core.qbank",
    "core.qbank_parse",
]

# 把 flet 系列包的数据文件（Flutter 运行时等）全部带上
for pkg in ("flet", "flet_desktop", "flet_web"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, "app", "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="批改模拟器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # ← 关键：不弹黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
