"""第一步：让 AI 出一道题，并起草评分细则。

【2026-09-29 新增 · 出题后的"答案泄漏"闸门】
出题时 AI 极容易把【做法】写进材料——"政府出台了……政策""引导……向……转移"
"大力发展……新兴产业"。而这些"做法"往往就是评分细则的因果链本身，
于是学生只要复述材料就能拿满分，全班答卷长得一模一样，这道题就废了。
（实测踩过：第 28 号存档第 1 份直接拿了 8 分满分。）

光靠提示词压不住（question.txt 里已写明红线，实测仍会漏），
所以在生成后补一道**纯本地的机械核对**：把每条采分点【冒号后的因果链】
拿去跟材料原文比，连续雷同超过阈值就判定"答案泄漏"，带着反馈打回重出。
不联网、不额外花钱（只在确实泄漏时才多花一次出题调用）。
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

from core.models import Question, RubricPoint
from services.prompts import fill, load_prompt

log = logging.getLogger(__name__)

# 从设问里兜底抽分值，如"（8分）""（10 分）"
_SCORE_IN_STEM = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]")

# 判定"答案泄漏"的连续雷同字数。
# 定 10 字：区域名、专有名词撞车一般不超过 6~8 字
# （"粤北、粤西""高新技术产业"都只有 6 字），10 字基本只能是一整句话被搬过去了。
LEAK_MIN_RUN = 10

# 材料与设问的分界标记（材料里可能出现"经济"这类词，所以不能拿整段题面比）
_ASK_MARKS = ("【设问】", "【问题】", "【小问】", "设问：", "问题：")

# ── 自然题 / 人文题 交替出（2026-10-02 用户要求）──────────────────────
# 用户反馈："现在都是人文大题，自然也要有"。
# 根子在主题清单本身偏人文——"产业转移""都市农业""飞地经济"这些题材
# 又像真题又好写，AI 就一路出人文题，连着好几轮都见不到自然地理。
# 光在提示词里补几个自然主题不够（AI 仍会挑它顺手的写），
# 所以让程序记住上一次出的是哪一类，这一次就出另一类。
TOPIC_TYPE_FILE = "last_topic_type.txt"

TOPIC_HINTS: Dict[str, str] = {
    "natural": (
        "\n\n⚠ 这次请出一【自然地理】的题：设问必须落在自然地理的原理上"
        "（地貌成因、河流水文／水系特征、气候成因、土壤与植被、"
        "自然生态过程、水循环……）。"
        "不要出产业转移／都市农业／区位分析这一类人文题。"
        "自然题同样要落到真实区域、结合材料里的数据与现象推理，"
        "不能出成「背原理就能答」的简答题。"
    ),
    "human": (
        "\n\n⚠ 这次请出一【人文地理】的题：设问落在人文地理的原理上"
        "（产业区位、产业转移、人口流动、交通与区域联系、城镇化……），"
        "同样要落到真实区域、结合材料数据推理。"
    ),
}


def _topic_type_path():
    from core.paths import writable

    return writable("data", TOPIC_TYPE_FILE)


def last_topic_type() -> str:
    """读出上一次出的题目类型（natural / human）；读不到返回空串。"""
    try:
        return _topic_type_path().read_text(encoding="utf-8").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def remember_topic_type(topic_type: str) -> None:
    """记下这次出的类型，供下一次"换一类"用。写失败不影响出题。"""
    try:
        p = _topic_type_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(topic_type, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("记录出题类型失败（不影响出题）：%s", e)


def pick_topic_type(cfg: dict, last: str | None = None) -> str:
    """决定这次出自然题还是人文题。

    配置 `question.topic_type`：
        auto（默认）= 与上一次相反（两类交替，不会连着出人文题）；
                      第一次运行（没有记录）时先出**自然题**
        natural     = 只出自然题
        human       = 只出人文题

    last 只给测试用；平时传 None，函数自己去读状态文件。
    """
    mode = str((cfg.get("question") or {}).get("topic_type", "auto")).strip().lower()
    if mode in ("natural", "自然", "自然类"):
        return "natural"
    if mode in ("human", "人文", "人文类"):
        return "human"
    prev = last if last is not None else last_topic_type()
    return "human" if prev == "natural" else "natural"


def _normalize(data: dict, subject: str, point_score: float) -> Question:
    stem = str(data.get("question", "")).strip()
    if not stem:
        raise ValueError("AI 没有返回题目正文")

    raw_points = data.get("points") or []
    if not raw_points:
        raise ValueError("AI 没有返回评分细则")

    points: List[RubricPoint] = []
    for i, p in enumerate(raw_points, start=1):
        if isinstance(p, str):
            text, score = p, point_score
        else:
            text = str(p.get("text", "")).strip()
            score = float(p.get("score", point_score) or point_score)
        if text:
            points.append(RubricPoint(seq=i, text=text, score=score))

    if not points:
        raise ValueError("评分细则为空")

    total = sum(p.score for p in points)
    # 满分以【设问里的分值】为准，不能按采分点合计来算：
    # 出题时会多给几个候补采分点（8 分的题配 5 个点，合计 10 分），
    # 但真实高考是"多答不加分"，满分仍是 8。
    max_score = float(data.get("max_score") or 0)
    if max_score <= 0:
        m = _SCORE_IN_STEM.search(stem)
        max_score = float(m.group(1)) if m else total
    if max_score > total:
        log.info("题面满分 %s 大于采分点合计 %s，按合计算", max_score, total)
        max_score = total
    elif total > max_score:
        log.info(
            "采分点合计 %s 分 > 满分 %s 分（多出的 %s 个是候补点，供教师筛选；"
            "学生全答对也只有 %s 分）",
            total, max_score, len(points) - round(max_score / point_score)
            if point_score else 0, max_score,
        )

    return Question(
        subject=str(data.get("subject") or subject),
        topic=str(data.get("topic") or "").strip(),
        stem=stem,
        max_score=max_score,
        points=points,
    )


def _tail_of(point_text: str) -> str:
    """取采分点【冒号后】的因果机制 + 落脚结论。

    采分点的统一格式是「条件／现象：因果机制、落脚结论」。
    冒号前的"条件"跟材料重合是正常的（学生要能从材料里指认出来），
    真正不能泄漏给材料的是冒号后的【因果链】——那才是学生该推的那一步。
    """
    for sep in ("：", ":"):
        if sep in point_text:
            tail = point_text.split(sep, 1)[1].strip()
            if tail:
                return tail
    return point_text


def _material_of(question: Question) -> str:
    """取出这道题的材料部分（设问不算）。

    ⚠ 2026-10-04：题库来的题材料在 question.material 里（不在 stem 里），
       所以先看 material；没有再退回从前那种"从 stem 里切"的老算法
       （AI 随机出题时代材料就写在 stem 里）。
    """
    if (question.material or "").strip():
        return question.material
    stem = question.stem or ""
    for mark in _ASK_MARKS:
        if mark in stem:
            return stem.split(mark, 1)[0]
    return stem


def rubric_leak_problems(question: Question, min_run: int = LEAK_MIN_RUN) -> List[str]:
    """检查评分细则的因果链有没有被直接写进材料（= 答案泄漏）。

    返回人类可读的问题列表；空列表表示没有泄漏。
    """
    from core.quality import longest_common

    material = _material_of(question)
    if not material.strip():
        return []

    out: List[str] = []
    for p in question.points:
        tail = _tail_of(p.text)
        if len(tail) < min_run:
            continue
        run = longest_common(tail, material)
        if run >= min_run:
            out.append(f"第{p.seq}点的因果链有 {run} 个字与材料原文雷同")
    return out


def _leak_feedback(problems: List[str]) -> str:
    """把泄漏问题写成给 AI 的重出指令（附在 user 消息后面）。"""
    return (
        "\n\n⚠ 刚才那一版【不合格，必须重新出】：评分细则的因果链被直接写进了材料，"
        "等于把答案送给了学生——学生只要复述材料就能拿满分，整班答卷会一模一样。\n"
        "具体问题：\n  "
        + "\n  ".join(problems)
        + "\n重出时务必做到：\n"
        "1. 材料里【不许出现「做法／措施」】：不要写「政府出台了……政策」"
        "「引导……向……转移」「大力发展……」「建设了……」「划定了……」这类内容；\n"
        "2. 材料只写【客观事实与结果性数据】（位置、自然条件、现象、"
        "面积／产量／比重／年份变化），只呈现「变化的结果」，不呈现「造成变化的手段」；\n"
        "3. 评分细则的因果链必须是学生【从材料数据推出来】的那一步，"
        f"文字上跟材料不能有任何连续 {LEAK_MIN_RUN} 字以上的重合；\n"
        "4. 设问问的那个「为什么」，材料里一个字都不许给出答案。"
    )


async def generate_question(client, cfg: dict) -> Question:
    q_cfg = cfg["question"]
    subject = q_cfg.get("subject", "地理")
    point_score = float(q_cfg.get("point_score", 2))
    options = q_cfg.get("score_options", [6, 8])
    score_options_text = "或".join(str(o) for o in options)
    # 候补采分点：在"分值 ÷ 每点分值"之外再多出几个，留给教师筛掉
    spare = max(0, int(q_cfg.get("spare_points", 0)))
    # 提示词里举的例子固定用最小分值，读者一眼能算明白
    example_score = int(min(options)) if options else 8
    need = int(example_score / point_score) if point_score else 4
    point_total = need + spare

    system = fill(
        load_prompt("question.txt"),
        subject=subject,
        point_score=int(point_score) if point_score == int(point_score) else point_score,
        score_options=score_options_text,
        spare_points=spare,
        point_total=point_total,
    )

    topic_type = pick_topic_type(cfg)
    log.info(
        "本次出题类型：%s（config 里 question.topic_type=%s）",
        "自然地理" if topic_type == "natural" else "人文地理",
        q_cfg.get("topic_type", "auto"),
    )
    base_user = "请生成一道新的高中" + subject + "综合题。" + TOPIC_HINTS[topic_type]
    user = base_user
    guard = bool(q_cfg.get("leak_guard", True))
    rounds = max(1, int(q_cfg.get("leak_retry_rounds", 2)))
    temperature = float(q_cfg.get("temperature", 1.2))
    question: Question | None = None
    problems: List[str] = []

    for round_no in range(1, rounds + 1):
        data = await client.chat_json(system=system, user=user, temperature=temperature)
        question = _normalize(data, subject, point_score)
        # 记下这次出的类型，下一次自动换另一类（自然 ↔ 人文交替）
        remember_topic_type(topic_type)
        if not guard:
            return question

        problems = rubric_leak_problems(question)
        if not problems:
            if round_no > 1:
                log.info("重新出题后答案泄漏已消除（第 %s 轮）", round_no)
            return question

        log.warning(
            "出题第 %s 轮：评分细则的因果链被写进了材料（每题都会让学生"
            "靠复述得分），正在重出——%s",
            round_no,
            "；".join(problems),
        )
        if round_no >= rounds:
            break
        user = base_user + _leak_feedback(problems)

    log.warning(
        "出题 %s 轮后仍有答案泄漏（%s）。这一版学生可以靠复述材料得分，"
        "请在「评分细则」页手工删掉材料里那些「做法」句，或改一道题。",
        rounds,
        "；".join(problems),
    )
    return question
