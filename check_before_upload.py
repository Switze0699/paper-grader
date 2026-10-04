"""上传 GitHub 前的安全自检：确认 .env 等敏感文件真的没被 git 跟踪。

用法（在项目根目录跑）：
    python check_before_upload.py

⚠ 这是"检查 git 会提交哪些文件"，不是检查磁盘上有什么 ——
    文件在磁盘上没关系，只有进了 git 才危险。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 这些文件绝对不能进 git
FORBIDDEN = [
    ".env",
    "api_key.txt",
    "graders.db",
    "批改模拟器.exe",
]

# 这些目录绝对不能进 git
# ⚠ 匹配时必须用 "文件名/前缀/" 这种带斜杠的形式，
#   否则 build_apk.bat 会被 build 前缀误伤（这坑踩过一次）。
FORBIDDEN_DIRS = [
    ".venv/", "venv/", "__pycache__/", ".git/", "build/", "dist/",
    "data/", "logs/", "导出/", ".workbuddy/", ".flet/", "legacy/",
    "prompts_before_apk/", "prompts_v1_stable/", "prompts_current_final/",
]

# 智谱密钥的真实形状
ZHIPU_KEY = re.compile(r"\b[0-9a-f]{32}\.[0-9a-f]{16}\b", re.I)


def git_files() -> list[str]:
    """git 打算提交的文件清单。"""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return []
        return [x for x in r.stdout.splitlines() if x.strip()]
    except Exception:
        return []


def main() -> int:
    print("=" * 62)
    print("  上传前安全自检")
    print("=" * 62)
    print()

    # 1. .gitignore 在不在
    gi = ROOT / ".gitignore"
    print("[1] .gitignore")
    if gi.exists():
        print("    ✓ 存在")
    else:
        print("    ✗ 不存在！必须先建，否则密钥会被提交")
        return 1

    # 2. 规则里有没有 .env
    txt = gi.read_text(encoding="utf-8", errors="replace")
    print()
    print("[2] .gitignore 的关键规则")
    rules = [(".env 被忽略", r"^\.env$" in txt or "\n.env\n" in txt),
             (".env.example 仍然保留（模板要有）", "!\\.env\\.example" in txt
              or "!.env.example" in txt),
             (".venv/ 被忽略", ".venv/" in txt),
             ("data/ 被忽略（学生数据）", "data/" in txt),
             ("导出/ 被忽略", "导出/" in txt),
             (".workbuddy/ 被忽略", ".workbuddy/" in txt)]
    for name, ok in rules:
        print(f"    {'✓' if ok else '✗'} {name}")

    # 3. git 实际会不会提交 .env
    print()
    print("[3] git 实际跟踪的文件")
    files = git_files()
    if not files:
        print("    （还没执行 git add，或不是 git 仓库）")
    else:
        print(f"    共 {len(files)} 个文件")
        bad = [f for f in files if f in FORBIDDEN]
        bad += [f for f in files if any(f.startswith(d) for d in FORBIDDEN_DIRS)]
        if bad:
            print("    ⚠ 以下敏感文件被跟踪了，必须处理：")
            for b in sorted(set(bad))[:20]:
                print("      -", b)
        else:
            print("    ✓ 没有敏感文件被跟踪")

    # 4. 待提交文件里有没有真密钥
    print()
    print("[4] 待提交内容里是否含真密钥")
    found = []
    skip_ext = {".ttf", ".png", ".xlsx", ".db", ".exe", ".zip", ".ico"}
    for f in files:
        p = ROOT / f
        if not p.is_file() or p.suffix.lower() in skip_ext:
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if ZHIPU_KEY.search(content):
            found.append(f)
    if found:
        print("    ✗ 这些文件里有形似真密钥的内容，绝不能提交：")
        for f in found:
            print("      -", f)
    else:
        print("    ✓ 没发现真密钥")

    # 5. 体积检查
    print()
    print("[5] 大文件检查（GitHub 单文件上限 100 MB）")
    big = []
    for f in files:
        p = ROOT / f
        try:
            if p.is_file() and p.stat().st_size > 50 * 1024 * 1024:
                big.append((f, p.stat().st_size))
        except Exception:
            pass
    if big:
        print("    ⚠ 超过 50 MB 的文件：")
        for f, s in big:
            print(f"      - {f}  {s/1048576:.0f} MB")
    else:
        print("    ✓ 没有超大文件")

    # 总结
    print()
    print("=" * 62)
    ok = (not files) or (not [f for f in files if f in FORBIDDEN]
                         and not [f for f in files
                                  if any(f.startswith(d) for d in FORBIDDEN_DIRS)]
                         and not found and not big)
    if found or big or (files and [f for f in files if f in FORBIDDEN]):
        print("  结果：❌ 有安全问题，先处理上面列出的项")
    else:
        print("  结果：✅ 可以安全提交")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
