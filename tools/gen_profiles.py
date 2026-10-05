# -*- coding: utf-8 -*-
"""按 student_profiles.json 的字段设计，批量生成 60 份考生档案。

改完下面的配额表重跑即可覆盖 student_profiles.json（会先自动备份）。
档案里的 role_hint 是唯一进生成端提示词的字段，所以这里的措辞要具体、
可模仿——只给错误类型的名字，AI 是抓不住的（实测：抽象指令会被无视）。

用法：
    .venv/Scripts/python.exe tools/gen_profiles.py
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "student_profiles.json"
BACKUP = ROOT / "student_profiles.template.bak.json"

# ---------------------------------------------------------------- 配额表
# 总量必须 = 60。水平分布按用户 2026-10-05 指示。
QUOTA = {"优秀": 6, "良好": 8, "中等": 20, "薄弱": 16, "很差": 10}

# 三个示例档案原地保留（id / name / 内容都不动），这里只做校验用
KEEP_SAMPLES = {1, 2, 3}

# 错误条数配额，跟 core/planner.py::WRONG_QUOTA 保持一致
WRONG_QUOTA = {"优秀": (0, 0), "良好": (0, 1), "中等": (1, 2), "薄弱": (2, 3), "很差": (2, 3)}

# 建议答出的点数范围（soft，不是硬指令）
POINTS_RANGE = {"优秀": (4, 5), "良好": (4, 5), "中等": (3, 4), "薄弱": (2, 3), "很差": (1, 2)}

# 格式倾向：绝大多数人分点，少数人写长段
FORMAT_WEIGHTS = {"分点": 0.82, "长段": 0.18}
# 字数偏好按水平偏向
LENGTH_WEIGHTS = {
    "优秀": {"多": 0.55, "中": 0.40, "少": 0.05},
    "良好": {"多": 0.30, "中": 0.55, "少": 0.15},
    "中等": {"多": 0.12, "中": 0.55, "少": 0.33},
    "薄弱": {"多": 0.06, "中": 0.44, "少": 0.50},
    "很差": {"多": 0.04, "中": 0.31, "少": 0.65},
}

MODULES = ["自然", "人文", "区域", "图表"]
ERRORS = ["答非所问", "因果颠倒", "张冠李戴", "漏点", "堆材料"]
# 这三类是"注入到某一条"里的硬错误；另外两类是整体卷面倾向
INJECTABLE = ("答非所问", "因果颠倒", "张冠李戴")

# ---------------------------------------------------------------- 措辞库
OPENERS: Dict[str, List[str]] = {
    "优秀": [
        "你是一名地理基础非常扎实的高中生，读材料能一眼抓住关键信息，脑子里有完整的地理逻辑。",
        "你地理成绩拔尖，知识点记得牢、分析能力也强，作答时敢下判断，每条都把来龙去脉写清楚。",
        "你是班里的地理尖子，熟悉区域背景和基本原理，遇到综合题也能把几条线索串起来讲。",
        "你基础扎实、答题习惯好，能主动补上题目没明说但必须写的那层分析。",
        "你地理能力很强，读题准、术语规范，卷面条理清楚，从不靠猜。",
        "你成绩优秀，善于调用课堂上学过的地理过程，写答案时习惯把因果链补全。",
    ],
    "良好": [
        "你是一名地理基础较好的高中生，大部分设问你能答到点子上，因果关系也想得到一层半。",
        "你地理成绩良好，常规问题拿得稳，遇到需要综合判断的题目会犹豫一下。",
        "你基础不错，常规原理掌握得比较牢，但遇上冷门角度就容易卡壳。",
        "你地理能力中上，读材料能读懂，答得完整但偶尔漏掉某个方向。",
        "你成绩较好，答题有条理，只是有时写得太简，机制那层懒得展开。",
        "你基础比较扎实，一般的因果链能自己推出来，但不太爱写长分析。",
        "你地理水平中上，知识点记得住，稍微提示一下就能答完整。",
        "你成绩稳定在良好档，熟悉主干知识，细节和边界情况会漏。",
    ],
    "中等": [
        "你是一名地理成绩中等的学生。你能答出大部分题目的核心事实，术语也基本用对，但不擅长往下推一步说理。",
        "你地理水平中等，知识点记得零散，能把现象写出来，说不到原因。",
        "你成绩中等，基础概念懂一点，但一要综合就抓不住重点。",
        "你地理中等偏下，见过的题型能做，没见过的就照着套。",
        "你能答出题目问的那个方向，但说不清背后的地理原理。",
        "你基础一般，凭印象和套路答题，遇到需要推理的设问会卡住。",
        "你成绩中等，脑子里有一些零散知识，但拼不成完整的链条。",
        "你地理水平一般，答题时习惯先写现象，机制部分随便补一句。",
        "你能想起相关的地理原理，但不会把它跟题目材料里的具体条件对上。",
        "你成绩中等偏上，粗看答得挺全，细看有一半没说到根子上。",
        "你基础知识不差，但分析能力弱，读完材料抓不住它想让你答什么。",
        "你地理成绩一般，答对的部分表述清楚，答错的部分自己看不出来。",
        "你习惯用套路答题，遇到题目换了个问法就不适应。",
        "你水平中等，脑子里有印象，但把印象组织成有逻辑的答案对你来说很难。",
        "你成绩中等，能想到几条相关的因素，但不会把它们串成因果链。",
        "你地理一般，答得出来的都是书上原话，联系材料的能力不足。",
        "你中等水平，对常见设问有把握，对没见过的角度就没话说。",
        "你成绩中等偏下，靠死记硬背的知识点撑，遇到理解型问题就露怯。",
        "你地理水平中等偏下，能写对现象，说不对原因。",
        "你成绩中等偏上，粗心的地方在于写完现象就停笔，把「所以会怎么样」那一层省了。",
    ],
    "薄弱": [
        "你是一名地理基础薄弱的学生。常见地理原理还记得零星几个，一问原因就说不清，只能答出表面的东西。",
        "你地理成绩偏差，读材料抓不住重点，答题全凭印象。",
        "你基础知识不牢，只能想起零散片段，经不起追问。",
        "你地理水平偏低，对题目问的方向经常理解偏，写完才发现答的不是它要的。",
        "你成绩偏差，脑子里有一些模糊印象，但不成体系。",
        "你基础薄弱，读懂题意这一步就已经很吃力，只能硬套模板。",
        "你地理能力弱，涉及原理分析的部分基本空白。",
        "你成绩偏差，习惯把学过的那几个原理往上堆，不管对不对题。",
        "你水平偏低，好一点的题目能写个大概，再深一层就完全停了。",
        "你基础不牢，地理术语会用几个，但说不清它到底指什么。",
        "你地理成绩偏差，写字很用力但方向经常错，自己看不出。",
        "你水平偏差，答全靠模板，遇到没见过的设问就只写得下一两条。",
        "你基础知识薄弱，材料里的关键信息抓不住，只能凭常识编。",
        "你成绩偏差，分析能力几乎空白，只能复述材料里出现过的东西。",
        "你地理水平低，对原理的记忆是零碎的，凑不成推理。",
        "你水平偏差，写得出来的都是不成体系的话，说服力很弱。",
    ],
    "很差": [
        "你是一名地理基础很差的学生。这次考试你几乎没复习，脑子里只有一些零散、模糊的地理印象。",
        "你地理成绩靠后，连最基本的原理都说不完整，写答案全靠猜。",
        "你基础很差，能把句子写通顺、用上地理书面语，但内容经常是错的。",
        "你成绩很差，对题目的设问理解很偏，讨论的方向经常跟要求不是一回事。",
        "你地理几乎等于没学，能想起的只有几个听起来像术语的名词。",
        "你水平很低，写字还过得去，内容基本经不起看。",
        "你成绩差，答题时会把不相干的地理原理硬套过来，听着专业其实荒谬。",
        "你基础很差，题目问什么、你要答什么，经常分不清。",
        "你地理能力很弱，脑子里没有成体系的知识，只有零散印象。",
        "你成绩很差，写出来的东西要么是空的，要么是歪的，你自己察觉不到。",
    ],
}

WEAK_BY_MODULE = {
    "自然": "自然地理（地貌、水文、气候、植被这些）你答不全，容易漏角度",
    "人文": "人文地理（农业、工业、城市、人口这些）你答不全，容易漏角度",
    "区域": "区域综合分析（一个区域的优势短板、产业、生态问题）你答不全，容易漏角度",
    "图表": "图表和数据的判读（曲线、柱状图、示意图）你读得慢，容易漏信息",
}

# 优秀档的"薄弱"只能叫"稍弱"，否则跟"尖子生"自相矛盾（实测踩过）
WEAK_BY_MODULE_TOP = {
    "自然": "自然地理（地貌、水文、气候、植被）相对弱一些，遇到偏冷门的过程会犹豫",
    "人文": "人文地理（农业、工业、城市、人口）相对弱一些，区位分析的思路不够顺",
    "区域": "区域综合分析（区域优势短板、产业、生态问题）相对弱一些，答题时容易漏掉一两个角度",
    "图表": "图表和数据的判读（曲线、柱状图、示意图）相对弱一些，需要多花点时间读",
}

ERR_DESC = {
    "答非所问": "讨论的方向跟设问不是一回事——内容本身是地理知识，但答的不是题目问的那件事",
    "因果颠倒": "把因果关系说反，把结果当成原因",
    "张冠李戴": "把别处才成立的地理原理搬过来套，听着专业，其实不对",
    "漏点": "有一块内容你压根儿想不起来，卷面上就空着",
    "堆材料": "把材料里的现象堆上去凑数，但没往设问上答",
}

FORMAT_DESC = {
    "分点": "你习惯用「1. 2. 3.」分点写，一条一个意思",
    "长段": "你习惯写成一大段，不分点，条与条之间界限不清",
}

LENGTH_DESC = {
    "少": "写得省，每条两三句就收，全篇不长",
    "中": "篇幅中等，每条三四句",
    "多": "写得很满，每条都要把话说完",
}

# ★ 0 条错误时的收尾指令：必须是【具体、可执行】的要求，不能是"留一两处没想透"这种模糊话
# 用户 2026-10-05 明确要求：模糊指令 AI 抓不住，要么写空话，要么干脆写成满分卷。
CLOSER_NO_WRONG = {
    "优秀": (
        "整份答卷里允许有一处小瑕疵，而且只允许一处，请挑一条落实："
        "漏写一个次要的论据（主论据必须写全），"
        "或者某个地理术语用得不够精准（用词偏口语、不够规范），"
        "或者某一条只点到现象、没有把后面那层分析写透。"
        "除此之外，每条都要写对、写全、写上因果。"
    ),
    "良好": (
        "这份答卷没有明显写错的条目，但你要在其中一条上留一处不完整的痕迹，"
        "二选一：某一条只写了现象、漏掉了「所以会怎么样」那层分析；"
        "或某一条方向正确但论据写得单薄（一句话带过）。"
        "这一条仍然是【方向正确】的，只是没写透，绝不许把它写成错的。"
        "其余条目要写对写全。"
    ),
}

# 有明显错误时，各档的收尾（跟 0 条错误时的口径不冲突）
CLOSERS = {
    "优秀": "写错的条目数为 0。你对自己的答案是有把握的。",
    "良好": "错误要听着像你自己想出来的，严禁写成一眼看出的蠢话。",
    "中等": "错误要听着有道理，严禁写成一眼看出的蠢话；你察觉不到这些错误。",
    "薄弱": "错误要像你自己想出来的，严禁写成一眼看出的蠢话；你自己完全看不出来。",
    "很差": "你自己察觉不到这些错误——考完还会觉得题目问的挺清楚。",
}


def _weighted(rng: random.Random, weights: Dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _pick_modules(rng: random.Random, level: str) -> List[str]:
    if level == "优秀":
        n = rng.choices([0, 1], weights=[0.55, 0.45], k=1)[0]
    elif level == "良好":
        n = rng.choices([0, 1], weights=[0.30, 0.70], k=1)[0]
    elif level == "中等":
        n = rng.choices([1, 2], weights=[0.60, 0.40], k=1)[0]
    elif level == "薄弱":
        n = rng.choices([1, 2], weights=[0.50, 0.50], k=1)[0]
    else:
        n = rng.choices([1, 2, 3], weights=[0.34, 0.46, 0.20], k=1)[0]
    if n == 0:
        return []
    return rng.sample(MODULES, n)


def _pick_errors(rng: random.Random, level: str) -> List[str]:
    """抽这一份的"错误倾向"。

    五类里只有三类（答非所问/因果颠倒/张冠李戴）是"某一条写错了"，
    会占用错误条数配额；另外两类（漏点/堆材料）是整体卷面形态，
    只写进 role_hint 的形态描述，不计入"写错的 N 条"。
    """
    lo, hi = WRONG_QUOTA[level]
    n = rng.randint(lo, hi)
    # 先抽可注入的错误条数（占配额）
    injectable = rng.sample(list(INJECTABLE), min(n, len(INJECTABLE)))
    errs = list(injectable)
    # 好学生不会出现"想不起来一块""堆材料凑数"这种卷面形态
    if level in ("薄弱", "很差") and rng.random() < 0.45:
        errs.append(rng.choice(["漏点", "堆材料"]))
    if level == "中等" and rng.random() < 0.22:
        errs.append("堆材料")
    return errs


def _role_hint(
    rng: random.Random,
    level: str,
    weak: List[str],
    errs: List[str],
    fmt: str,
    length_pref: str,
) -> str:
    parts: List[str] = [rng.choice(OPENERS[level])]

    if weak:
        # 优秀档不能用"完全没思路"这种话，否则和"尖子生"人设打架
        table = WEAK_BY_MODULE_TOP if level == "优秀" else WEAK_BY_MODULE
        # ⚠ 2026-10-05 第 66 批：薄弱模块【只影响答得全不全】，不影响答错什么。
        #   不说清这一点，AI 会把"这块没思路"理解成"这块可以随便乱写"，
        #   于是把错误内容也往薄弱模块上堆（甚至串到别的学科领域去）。
        #   真正决定错误内容的是【题目类型 + 错误倾向】，跟薄弱模块无关。
        parts.append(
            "你相对弱的地方（只影响你答得【全不全】，"
            "不影响你答错什么）：" + "、".join(table[m] for m in weak) + "。"
        )

    parts.append(FORMAT_DESC[fmt] + "，" + LENGTH_DESC[length_pref] + "。")

    # 卷面形态问题（漏点 / 堆材料）不是某一条写错，单独说
    soft = [e for e in errs if e not in INJECTABLE]
    if "漏点" in soft:
        parts.append("整份答卷里有一块内容你压根儿想不起来，那一块就空着，不要硬编。")
    if "堆材料" in soft:
        parts.append("你会把材料里的现象堆上去凑数，但没有往设问上答。")

    # 可注入的错误：必须点名到"第几条"，否则 AI 会全写对
    injectable = [e for e in errs if e in INJECTABLE]
    if not injectable:
        parts.append(CLOSER_NO_WRONG[level])
        return "".join(parts)

    pairs = "；".join(f"一条是{k}——{ERR_DESC[k]}" for k in injectable)
    parts.append(
        f"这份答卷里有 {len(injectable)} 条要写成错的（"
        + "、".join(injectable)
        + f"）：{pairs}。"
    )
    parts.append(CLOSERS[level])
    parts.append("其余条目是你水平该有的样子：不要写成残卷，也不要写成满分答卷。")
    return "".join(parts)


def _names(rng: random.Random, n: int) -> List[str]:
    xing = list(
        "李王张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋"
        "蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    )
    ming = list(
        "嘉禾雨桐亦航若曦昱辰梦琪浩然语嫣博文一诺芷晴皓轩梓涵可馨俊杰欣怡思远雨萱建勋天佑静怡宇轩沐辰"
        "若涵嘉豪诗涵致远芷若晨露子豪雅雯皓宇泽宇佳琪梦瑶逸辰明轩念慈永宁其琛开阳志远向阳沐阳若初念祖承泽"
        "清歌安然安然雨墨书瑶知遥听澜念慈惜缘若涵寒雨秋实嘉木长风锦书松柏溪云清越新翰逸凡"
    )
    out: List[str] = []
    seen = set()
    guard = 0
    while len(out) < n and guard < 4000:
        guard += 1
        nm = rng.choice(xing) + rng.choice(ming)
        if nm in seen:
            continue
        seen.add(nm)
        out.append(nm)
    return out


def build() -> List[dict]:
    rng = random.Random(20261005)

    doc = json.loads(TARGET.read_text(encoding="utf-8"))
    samples = {p["id"]: p for p in doc["profiles"] if p["id"] in KEEP_SAMPLES}
    if len(samples) != len(KEEP_SAMPLES):
        raise SystemExit("模板里的三个示例档案（id 1/2/3）没找全，先检查 student_profiles.json")

    names = _names(rng, 60)
    total = sum(QUOTA.values())
    if total != 60:
        raise SystemExit(f"配额合计是 {total}，不是 60")

    # ★ 示例 #1 是用户手写的，只补收尾的"小瑕疵"要求，其余一个字不改。
    # ⚠ 必须做幂等判断——重跑生成器时不能把同一段要求追加两次。
    top = dict(samples[1])
    if "小瑕疵" not in top["role_hint"]:
        top["role_hint"] = top["role_hint"].rstrip() + CLOSER_NO_WRONG["优秀"]
    samples[1] = top

    profiles: List[dict] = []
    used_names = set()
    next_id = 4

    for level, count in QUOTA.items():
        for k in range(count):
            if level == "优秀" and k == 0:
                profiles.append(samples[1])
                used_names.add(samples[1]["name"])
                continue
            if level == "中等" and k == 0:
                profiles.append(samples[2])
                used_names.add(samples[2]["name"])
                continue
            if level == "很差" and k == 0:
                profiles.append(samples[3])
                used_names.add(samples[3]["name"])
                continue

            nm = next(n for n in names if n not in used_names)
            used_names.add(nm)

            weak = _pick_modules(rng, level)
            errs = _pick_errors(rng, level)
            fmt = _weighted(rng, FORMAT_WEIGHTS)
            length_pref = _weighted(rng, LENGTH_WEIGHTS[level])
            lo, hi = POINTS_RANGE[level]

            profiles.append(
                {
                    "id": next_id,
                    "name": nm,
                    "level": level,
                    "weak_modules": weak,
                    "error_tendencies": errs,
                    "answer_habits": {"format": fmt, "length_pref": length_pref},
                    "role_hint": _role_hint(
                        rng, level, weak, errs, fmt, length_pref
                    ),
                    "expected_points_covered": rng.randint(lo, hi),
                }
            )
            next_id += 1

    profiles.sort(key=lambda p: p["id"])
    assert len(profiles) == 60, f"生成了 {len(profiles)} 份"
    assert [p["id"] for p in profiles] == list(range(1, 61)), "id 不连续"
    return profiles


def main() -> None:
    profiles = build()

    doc = json.loads(TARGET.read_text(encoding="utf-8"))
    doc["profiles"] = profiles
    doc["_说明"]["档案总数"] = len(profiles)
    doc["_说明"]["水平分布"] = {k: v for k, v in QUOTA.items()}
    doc["_说明"]["最后更新"] = "2026-10-05"

    if TARGET.exists():
        shutil.copy(TARGET, ROOT / "student_profiles.prev.json")

    TARGET.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    by_level: Dict[str, int] = {}
    for p in profiles:
        by_level[p["level"]] = by_level.get(p["level"], 0) + 1
    print(f"已写入 {TARGET}，共 {len(profiles)} 份")
    print("水平分布：", by_level)
    dup = [n for n in {p["name"] for p in profiles} if
           sum(1 for q in profiles if q["name"] == n) > 1]
    print("重名：", dup or "无")


if __name__ == "__main__":
    main()