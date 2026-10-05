# -*- coding: utf-8 -*-
"""考生档案库读取与抽样（2026-10-05 新增，纯新增文件）。

【它负责什么】
    1. 读 student_profiles.json 到内存
    2. 每次抽题后从这里抽 8 份档案，转成 planner.make_plans 能吃的 dict
    3. 保证抽出来的 8 份层次分布稳定（不纯随机，避免抽出 6 个中等）

【为什么单独一个文件】
    跟 core/qbank.py 一个思路：新功能独立成文件，不污染现有模块。
    出问题把这个文件删掉即可完全回退。

【⚠ 三道红线】
    1. name / level / weak_modules 一律【不进任何提示词】——
       阅卷端必须看不到名字，盲评才干净（用户明确要求）。
    2. 这个模块【只管生成端】。判分、导出、报告一行都不碰。
    3. 档案文件读不到时【不许崩】——返回空列表，调用方回退到旧的
       "按 weights 随机抽" 逻辑，程序照常跑。

【抽样配额从哪来】
    默认按"和总库同样的比例"缩放：总库 60 人 = 优6/良8/中20/薄16/差10，
    抽 8 份时放大后就是 优1/良1/中3/薄2/差1（合计 8）。
    比例算完向下取整，余数按"档位越靠中越优先"补齐，保证正好 8 份。
"""

from __future__ import annotations

import json
import logging
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

PROFILE_FILE = "student_profiles.json"

# ⚠ 判分端不认识这几个标签；这里只用来做分布控制。
LEVELS = ("优秀", "良好", "中等", "薄弱", "很差")

# 抽样时的档位优先序（配额有余额时，先补中间档——真实班级中等生最多）
_FILL_ORDER = ("中等", "薄弱", "很差", "良好", "优秀")


def _path(root: Optional[Path] = None) -> Path:
    """档案文件路径。

    ⚠ 用函数而不是模块级常量：模块级常量在 import 时就固定了，
      测试里想指到临时目录就改不动（题库那边踩过这个坑）。

    ⚠ 打包成 exe 后有两套位置，都要找：
      · 开发时：项目根（本文件的上上级）
      · exe 运行时：PyInstaller 解包目录（sys._MEIPASS）
      先找 exe 解包目录，再退回项目根——找不到就返回项目根那个（会走
      "不存在 → 退回随机分组"的分支，不崩）。
    """
    if root is not None:
        return Path(root) / PROFILE_FILE
    # exe 打包后：资源被解到 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / PROFILE_FILE
        if p.exists():
            return p
    # 开发时：项目根 = 本文件的上上级
    return Path(__file__).resolve().parent.parent / PROFILE_FILE


def load_profiles(root: Optional[Path] = None) -> List[dict]:
    """把档案库读进内存。读不到就返回空列表（**不抛异常**）。"""
    p = _path(root)
    if not p.exists():
        log.warning("考生档案库不存在：%s（将退回旧的随机分组）", p)
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.error("考生档案库解析失败：%s（将退回旧的随机分组）", e)
        return []

    raw = doc.get("profiles") or []
    out: List[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        lv = str(item.get("level") or "").strip()
        if lv not in LEVELS:
            log.warning("档案 #%s 的档位 %r 不认识，跳过", item.get("id"), lv)
            continue
        out.append(item)
    return out


def quota_for(count: int, profiles: List[dict]) -> Dict[str, int]:
    """按总库比例算出这次抽 count 份时，各档各要几个。

    返回的字典合计一定等于 count。

    ⚠ 2026-10-05 修正：纯按比例向下取整会出问题——
      总库 60 人、抽 8 份时，优秀 6/60×8=0.8→0、良好 8/60×8=1.07→1，
      结果**尖子生一个都抽不到**，答卷层次全挤在中间。
      改成"最大余额法"：先取整，再把余数分给"被截掉的小数部分最大"的档。
      这样 8 份的分布是 优1/良1/中3/薄2/差1，全校五档都能露面。
    """
    if not profiles or count <= 0:
        return {}
    total = len(profiles)
    have = {lv: sum(1 for p in profiles if p["level"] == lv) for lv in LEVELS}

    # 最大余额法：先算精确配额，取整数部分，余数按小数部分从大到小分
    exact = {lv: count * have[lv] / total for lv in LEVELS}
    quota = {lv: int(exact[lv]) for lv in LEVELS}
    # 不能超过该档实际人数
    for lv in LEVELS:
        quota[lv] = min(quota[lv], have[lv])

    rest = count - sum(quota.values())
    # 按小数部分降序补，同小数时中间档优先
    order = sorted(LEVELS, key=lambda lv: (-(exact[lv] - int(exact[lv])),
                                           _FILL_ORDER.index(lv)))
    guard = 0
    while rest > 0 and guard < 200:
        guard += 1
        moved = False
        for lv in order:
            if rest <= 0:
                break
            if quota[lv] < have[lv]:
                quota[lv] += 1
                rest -= 1
                moved = True
        if not moved:
            break      # 所有档位都抽完了（题库比 count 小）
    return {lv: v for lv, v in quota.items() if v > 0}


def pick_profiles(
    count: int = 8,
    root: Optional[Path] = None,
    rng: Optional[random.Random] = None,
) -> List[dict]:
    """抽 count 份档案，层次分布按总库比例。

    抽不出来（文件没有 / 库是空的）就返回空列表，调用方自己回退。
    """
    profiles = load_profiles(root)
    if not profiles:
        return []
    r = rng or random
    quota = quota_for(count, profiles)
    if not quota:
        return []

    picked: List[dict] = []
    for lv, n in quota.items():
        pool = [p for p in profiles if p["level"] == lv]
        picked.extend(r.sample(pool, min(n, len(pool))))

    # 库不够大时会少于 count，尽量从剩下的里再补
    if len(picked) < count:
        chosen = {id(p) for p in picked}
        rest = [p for p in profiles if id(p) not in chosen]
        r.shuffle(rest)
        picked.extend(rest[: count - len(picked)])

    return picked[:count]


def to_plan_kwargs(profiles: List[dict]) -> List[dict]:
    """把档案整理成 make_plans 能直接吃的形状（只挑它认识的字段）。

    ⚠ name / weak_modules 故意【不传】——它们不该出现在提示词里。
      weak_modules 的信息已经写进 role_hint 的自然语言描述里了。
    """
    out: List[dict] = []
    for p in profiles:
        habits = p.get("answer_habits") or {}
        out.append(
            {
                "profile_id": p.get("id"),
                "level": p.get("level"),
                # style 只用来挑 defects 的写法，档案格式固定用"分点"
                "style": "",
                "role_hint": p.get("role_hint") or "",
                "error_tendencies": list(p.get("error_tendencies") or []),
                "format": habits.get("format") or "分点",
                "length_pref": habits.get("length_pref") or "中",
            }
        )
    return out
