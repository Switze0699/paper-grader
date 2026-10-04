"""提示词读取与占位符填充。

占位符写法：{subject}、{question} 这样的一对花括号。
用 replace 而不是 str.format，这样提示词里可以随便写 JSON 的大括号。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from core import paths

# 打包后优先用 exe 旁边的 prompts 文件夹，没有就用程序内置的
PROMPT_DIR = paths.resource("prompts")


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"找不到提示词文件：{path}")
    return path.read_text(encoding="utf-8")


def fill(template: str, **kwargs) -> str:
    for key, value in kwargs.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def fill_many(mapping: Dict[str, str], **kwargs) -> Dict[str, str]:
    return {k: fill(v, **kwargs) for k, v in mapping.items()}
