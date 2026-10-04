"""配置读取与日志初始化。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core import paths

# 注意：打包成 exe 后，这些路径会自动指向 exe 所在的目录（见 core/paths.py）
ROOT = paths.user_root()
CONFIG_PATH = paths.resource("config.yaml")
LOG_DIR = paths.writable("logs")


def load_config() -> dict:
    paths.seed_user_files()  # exe 首次运行时把配置/提示词复制到旁边，方便用户改
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    load_dotenv(ROOT / ".env")
    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: dict) -> None:
    """允许在 .env 里临时覆盖接口地址和模型，不填就用 config.yaml 的默认值。"""
    ai = cfg.setdefault("ai", {})
    base_url = (os.getenv("AI_BASE_URL") or "").strip()
    model = (os.getenv("AI_MODEL") or "").strip()
    if base_url:
        ai["base_url"] = base_url
    if model:
        ai["model"] = model


# 依次尝试的环境变量名，第一个有值的生效
API_KEY_ENV_CANDIDATES = ("ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY")


def _key_file() -> str:
    """API Key 的备用存放位置（界面填密钥时用）。

    安卓上没有 .env 文件、也没法用记事本改，密钥只能在界面上填，
    填完存到这个文件里（app 私有目录，用户看不到也改不了）。
    """
    return str(ROOT / "api_key.txt")


def get_api_key(cfg: dict) -> str:
    """读取智谱的 API Key。顺序：环境变量 → .env → app 私有目录的密钥文件。"""
    load_dotenv(ROOT / ".env")
    env_name = cfg.get("ai", {}).get("api_key_env") or "ZHIPU_API_KEY"
    for name in (str(env_name),) + API_KEY_ENV_CANDIDATES:
        key = (os.getenv(name) or "").strip().strip('"').strip("'")
        if key:
            return key
    try:
        p = Path(_key_file())
        if p.exists():
            key = p.read_text(encoding="utf-8").strip().strip('"').strip("'")
            if key:
                return key
    except Exception:
        pass
    return ""


def save_api_key(key: str) -> None:
    """保存密钥到 app 私有目录（界面上"保存密钥"按钮用）。"""
    p = Path(_key_file())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((key or "").strip(), encoding="utf-8")


def setup_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")]
    # 打包成无控制台的 exe 后 sys.stdout 是 None，不能再挂控制台输出
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
