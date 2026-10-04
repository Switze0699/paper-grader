"""路径解析：同一套代码要能在三种环境里找到文件。

三种环境：
  1. 直接跑源码            → 根目录就是项目文件夹
  2. 打包成 Windows exe   → exe 所在目录（用户看得见、能改的地方）
  3. 打包成 Android APK    → 应用的私有数据目录（安卓上没有"exe 旁边"这种概念）

⚠ 安卓上最要命的一点：**`sys.executable` 不存在**，
直接拿它取目录会当场崩，所以必须先判断平台。
只读资源（提示词、字体）从 APK 内置那份读；
能改的东西（config.yaml、数据库、日志、导出）放应用私有目录。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 是否运行在打包后的 exe 里
FROZEN = bool(getattr(sys, "frozen", False))

_SRC_ROOT = Path(__file__).resolve().parent.parent


def _is_android() -> bool:
    """判断是不是跑在安卓上。

    CPython 3.13 在 Android 里会提供 sys.getandroid()；
    万一没有，就退回看环境变量（serious_python / PyInstaller 都会设）。
    """
    try:
        if hasattr(sys, "getandroid") and sys.getandroid():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_ARGUMENT"))


ANDROID = _is_android()


def _android_writable_root() -> Path:
    """安卓上的可写目录。取不到就退回当前目录，至少别崩。"""
    for env in ("FLET_APP_STORAGE_PATH", "ANDROID_APP_PATH", "ANDROID_PRIVATE"):
        v = os.environ.get(env)
        if v and Path(v).exists():
            return Path(v)
    try:
        home = Path.home()
        if str(home) and str(home) not in ("/", "."):
            return home
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


if ANDROID:
    # APK 里：资源从包内读，能改的放应用私有目录
    BUNDLE_DIR = _SRC_ROOT
    APP_DIR = _android_writable_root()
elif FROZEN:
    # exe 有两个目录：
    #   BUNDLE_DIR：程序内部解包目录（打包进去的默认文件，只读）
    #   APP_DIR：exe 所在目录，用户看得见、能改
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", _SRC_ROOT))
    try:
        APP_DIR = Path(sys.executable).resolve().parent
    except Exception:  # noqa: BLE001
        APP_DIR = _SRC_ROOT
else:
    BUNDLE_DIR = _SRC_ROOT
    APP_DIR = _SRC_ROOT


def user_root() -> Path:
    """用户可见的根目录（数据、日志、导出都放这里）。"""
    return APP_DIR


def resource(*parts: str) -> Path:
    """取一个只读资源：优先取可写目录旁边的，取不到用包内置的。

    ⚠ 安卓上只可能来自包内置那份（用户还没复制出去之前）。
    """
    external = APP_DIR.joinpath(*parts)
    if external.exists():
        return external
    return BUNDLE_DIR.joinpath(*parts)


def writable(*parts: str) -> Path:
    """取一个可写路径（安卓上永远是应用私有目录，不会崩）。"""
    p = APP_DIR.joinpath(*parts)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


# 首次运行时把这些默认文件复制到可写目录，方便用户自己改
_SEED_FILES = ("config.yaml", ".env.example")
_SEED_DIRS = ("prompts", "assets")


def seed_user_files() -> None:
    """把包内置的默认文件复制一份到可写目录。

    - 桌面上只在"跑 exe 且文件不在旁边"时做（首次运行）
    - 安卓上每次启动都做一次：App 升级后能把新版提示词带出来，
      但**不会覆盖已存在的文件**（否则用户改过的提示词会被冲掉）
    """
    if not FROZEN and not ANDROID:
        return
    try:
        for name in _SEED_FILES:
            src = BUNDLE_DIR / name
            dst = APP_DIR / name
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        for name in _SEED_DIRS:
            src = BUNDLE_DIR / name
            dst = APP_DIR / name
            if src.exists() and not dst.exists():
                shutil.copytree(src, dst)
    except Exception:  # noqa: BLE001
        # 复制失败也不影响运行，大不了用内置的
        pass
