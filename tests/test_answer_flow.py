"""答卷生成链路的离线集成测试。

【2026-09-29 起的新规则】生成学生答案时【只给题目 + 角色】，
【严禁把评分细则发给 AI】——它必须像真考生一样凭自己的知识作答。
本测试用一个"假 AI 客户端"专门验证这条：

  1. 发给 AI 的提示词里【不能出现】评分细则的任何内容（采分点原文）
  2. 每位学生拿到的是【自己的角色提示词】（水平 + 答题习惯）
  3. 跑方向的内容会被本地闸门打回、交给 AI 改写
  4. 答案压成残句也会被打回
  5. 全程不联网、不需要密钥、不花钱

运行方式：
    .venv\\Scripts\\python.exe tests\\test_answer_flow.py
"""

from __future__ import annotations

import asyncio
import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.models import Question, RubricPoint, StudentPlan  # noqa: E402
from services.answer_service import generate_answers  # noqa: E402

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  通过  {name}")
    else:
        print(f"  失败  {name} {extra}")
        FAILED.append(name)


POINTS = [
    RubricPoint(seq=1, text="上游落差大水流湍急：水能丰富，适宜梯级开发", score=2),
    RubricPoint(seq=2, text="流域降水丰富径流量大：河流水量充足，通航能力强", score=2),
    RubricPoint(seq=3, text="无结冰期：可全年通航，航运价值高", score=2),
]

QUESTION = Question(
    subject="地理", topic="河流水文特征",
    stem="【材料】某河段位于湿润山区。\n【设问】分析该河段的自然水文特征。（6分）",
    max_score=6, points=POINTS,
)

PLAN = StudentPlan(
    seq=1, ability="中等", style="浮于表面型",
    hit=[1], partial=[2], off=[3], blank=[], design_score=3.0,
)

CFG = {
    "classroom": {
        "answer_batch": 1,
        "answer_concurrency": 1,
        "answer_temperature": 0.85,
        "quality_gate": True,
        "regenerate_rounds": 1,
        "force_fix": False,
        "direction_guard": True,
        "forbidden_words": {
            "natural": ["经济", "人口", "劳动力"],
            "favorable": ["限制", "制约", "不足"],
        },
    }
}

# 一份"像学生写的"答案（题目问自然特征，就不会出现经济、人口这类词）
# ⚠ 2026-09-29 起篇幅口径拉平：五档都要写满，答案本来就更长，
#   所以这里的样例也按"真学生写满答题卡"的样子给足篇幅。
GOOD = (
    "1. 该河段位于湿润山区，上游落差比较大，河流的流速较快。\n"
    "2. 流域内的降水比较多，河流的补给量比较大，水量总体比较充足。\n"
    "3. 该河段冬季的气温比较高，河流没有结冰期，可以全年通航。\n"
    "4. 河流的水量比较丰富，水位的变化相对不大，航运条件比较好。"
)

# 跑方向的答案：题目问自然特征，它写成了经济、人口（本地闸门该打回）
OFF_DIRECTION = (
    "1. 该地区经济发达，人口密集，运输需求量大。\n"
    "2. 沿岸劳动力充足，适合发展加工业。\n"
    "3. 当地政府出台政策支持水运发展。"
)

# 残句答案：压成了几个词（本地闸门该打回）
TOO_SHORT = "1. 落差大。\n2. 水多。\n3. 不结冰。"

# 被改写后的"规范版"（分点完整、方向对、篇幅写足）
FIXED = (
    "1. 该河段上游落差比较大，水流的流速比较湍急。\n"
    "2. 流域内的降水比较丰富，河流的水量比较充足，补给比较稳定。\n"
    "3. 该河段冬季不结冰，没有明显的结冰期，通航的时间比较长。\n"
    "4. 河流的水量比较丰富，河道的宽度也比较大，航运的条件比较好。"
)

# 篇幅明显比别人短（但仍过了"绝对下限"）——用来验证全班篇幅均衡
SHORT_OK = (
    "1. 该河段上游落差比较大，水流的速度比较快。\n"
    "2. 流域内的降水比较多，河流的水量比较大。\n"
    "3. 该河段冬季不结冰，可以全年通航。"
)

# 篇幅正常的答卷（约 195 字）
LONG_OK = (
    "1. 该河段位于湿润的山区，上游河段的落差比较大，流速比较湍急，"
    "水流的侵蚀作用比较明显。\n"
    "2. 流域内的降水比较丰富，河流的补给量比较大，水量总体比较充足，"
    "水位的变化相对比较平缓。\n"
    "3. 该河段冬季的气温比较高，河流没有明显的结冰期，一年四季都可以通航，"
    "航运的时间比较长。\n"
    "4. 河流的水量比较丰富而且比较稳定，河道的宽度也比较大，"
    "适合船舶通行，航运的条件比较好。"
)


class StubClient:
    """假 AI 客户端：记录收到的提示词，并按剧本返回答案。"""

    def __init__(self, gen_answer: str = GOOD, fix_answer: str = FIXED) -> None:
        self.gen_answer = gen_answer
        self.fix_answer = fix_answer
        self.gen_prompts: list = []
        self.fix_prompts: list = []

    async def chat_json(self, system: str, user: str, temperature: float = 0.0):
        if "就地修改" in user:          # 改写调用（answer_fix.txt）
            self.fix_prompts.append((system, user))
            return {"answer": self.fix_answer}
        self.gen_prompts.append((system, user))   # 生成调用
        return {"answer": self.gen_answer}


class SeqClient(StubClient):
    """按【学生编号】返回不同长度的答案（用来验证全班篇幅均衡）。"""

    def __init__(self, by_seq: dict, fix_answer: str = FIXED) -> None:
        super().__init__(fix_answer=fix_answer)
        self.by_seq = by_seq

    async def chat_json(self, system: str, user: str, temperature: float = 0.0):
        if "就地修改" in user:
            self.fix_prompts.append((system, user))
            return {"answer": self.fix_answer}
        self.gen_prompts.append((system, user))
        m = re.search(r"学生编号：(\d+)", user)
        seq = int(m.group(1)) if m else 0
        return {"answer": self.by_seq.get(seq, GOOD)}


def run_flow(gen_answer: str = GOOD, fix_answer: str = FIXED,
             plans=None, cfg=None):
    stub = StubClient(gen_answer, fix_answer)
    plans = plans if plans is not None else [PLAN]
    students = asyncio.run(
        generate_answers(stub, cfg or copy.deepcopy(CFG), QUESTION, plans)
    )
    return stub, students


def test_no_rubric_leak():
    """最核心的一条：生成时不许把评分细则发给 AI。"""
    print("\n[生成时不发评分细则]")
    stub, students = run_flow()
    check("生成调用了一次（每人一次请求）", len(stub.gen_prompts) == 1,
          str(len(stub.gen_prompts)))
    system, user = stub.gen_prompts[0]
    whole = system + user
    check("提示词里有题目设问", "分析该河段的自然水文特征" in whole)
    leaked = [p.text for p in POINTS if p.text in whole]
    check("采分点原文没有泄漏给 AI", not leaked, str(leaked))
    check("没有把细则当成参考答案递过去",
          "参考答案" not in whole and "评分标准（满分" not in whole)
    check("没有出现「采分点」字样", "采分点" not in whole)
    check("没有出现「完整命中／部分命中」这类内部档位词", "命中" not in whole)
    check("没有出现「要漏掉哪段因果」这类逐点指令",
          "漏掉哪段" not in whole and "因果链" not in whole)
    # ★2026-10-05：差生书面语铁律（用户实测第52 份里"大家喜欢""买买东西"）
    check("生成端写明'差生不等于没上过高中'",
          "差生不等于没上过高中" in whole)
    check("生成端把书面语列为硬红线且五档都适用",
          "书面语是硬红线" in whole and "五档全部适用" in whole)
    check("生成端明确禁掉日常口语例子",
          "大家喜欢" in whole and "买买东西" in whole)
    check("生成端写明'水平差不体现在说话像不像学生'",
          "不体现在说话像不像学生" in whole)
    check("生成端写明'写得少≠ 说大白话'",
          "写得少 ≠ 说大白话" in whole)
    check("生成端把口语列入'不允许的错误'",
          "不允许的错误" in whole and "说大白话" in whole)
    check("明确告诉 AI 它看不到标准答案",
          "没有标准答案" in whole or "没有人会把评分细则给你" in whole)


def test_role_prompt():
    """学生水平靠角色提示词控制：每人拿到自己的角色。"""
    print("\n[角色提示词]")
    plans = [
        StudentPlan(seq=1, ability="优秀", style="扎实型", design_score=5.4),
        StudentPlan(seq=2, ability="中等", style="浮于表面型", design_score=3.0),
        StudentPlan(seq=3, ability="很差", style="半途而止型", design_score=0.6),
    ]
    stub, students = run_flow(plans=plans)
    check("三位学生各调用一次", len(stub.gen_prompts) == 3, str(len(stub.gen_prompts)))
    prompts = [u for _s, u in stub.gen_prompts]
    check("优秀档的角色提示词写进了提示词",
          any("基础扎实" in p and "水平：优秀" in p for p in prompts))
    check("中等档的角色提示词写进了提示词",
          any("成绩中等" in p and "水平：中等" in p for p in prompts))
    check("很差档的角色提示词写进了提示词",
          any("基础很差" in p and "水平：很差" in p for p in prompts))
    check("答题习惯也写进去了（浮于表面型）",
          any("浮于表面" in p for p in prompts))
    check("三份提示词互不相同", len(set(prompts)) == 3)
    check("每份都带篇幅参考", all("篇幅参考" in p for p in prompts))


def test_direction_gate():
    """跑方向的答案会被本地闸门打回并改写。"""
    print("\n[方向红线闸门]")
    stub, students = run_flow(gen_answer=OFF_DIRECTION, fix_answer=FIXED)
    check("跑方向的答案触发了改写", len(stub.fix_prompts) >= 1,
          str(len(stub.fix_prompts)))
    check("改写提示词里说清了问题",
          bool(stub.fix_prompts) and "方向" in stub.fix_prompts[0][1],
          str(stub.fix_prompts[0][1][:120]) if stub.fix_prompts else "")
    check("改写时同样没有给评分细则",
          bool(stub.fix_prompts)
          and not any(p.text in (stub.fix_prompts[0][0] + stub.fix_prompts[0][1])
                      for p in POINTS))
    check("最终答案里没有违禁词",
          "经济" not in students[0].answer and "人口" not in students[0].answer,
          students[0].answer)


def test_length_gate():
    """压成残句的答案会被打回。"""
    print("\n[篇幅闸门]")
    stub, students = run_flow(gen_answer=TOO_SHORT, fix_answer=FIXED)
    check("残句触发了改写", len(stub.fix_prompts) >= 1, str(len(stub.fix_prompts)))
    check("改写后采用了改好的版本",
          students[0].answer.strip() == FIXED.strip(),
          students[0].answer)


def test_class_balance_gate():
    """全班篇幅均衡：不许"好学生写一堆、差生一点不写"。"""
    print("\n[全班篇幅均衡]")
    from core.quality import class_length_problem

    # ① 纯函数：一份明显比别人短 → 只有它被点出来
    r = class_length_problem({1: SHORT_OK, 2: LONG_OK, 3: LONG_OK, 4: LONG_OK})
    check("明显偏短的那份被点出来", set(r) == {1}, str(list(r)))
    # ② 纯函数：一份明显比别人长 → 只有它被点出来
    r2 = class_length_problem({1: LONG_OK, 2: SHORT_OK, 3: SHORT_OK, 4: SHORT_OK})
    check("明显偏长的那份被点出来", set(r2) == {1}, str(list(r2)))
    # ③ 纯函数：篇幅整齐 → 不报问题
    r3 = class_length_problem({1: LONG_OK, 2: LONG_OK, 3: GOOD, 4: LONG_OK})
    check("篇幅整齐时不报问题", r3 == {}, str(r3))

    # ④ 集成：同一批里偏短的答卷会被打回改写，其余原样保留
    plans = [
        StudentPlan(seq=i, ability="中等", style="浮于表面型", design_score=3.0)
        for i in range(1, 5)
    ]
    stub = SeqClient({1: SHORT_OK, 2: LONG_OK, 3: LONG_OK, 4: LONG_OK})
    students = asyncio.run(
        generate_answers(stub, copy.deepcopy(CFG), QUESTION, plans)
    )
    check("只有偏短的那份触发了改写", len(stub.fix_prompts) == 1,
          str(len(stub.fix_prompts)))
    if stub.fix_prompts:
        check("改写提示词里说清了是篇幅问题",
              "篇幅" in stub.fix_prompts[0][1], stub.fix_prompts[0][1][:160])
    check("改写后那一份补长了",
          len(students[0].answer) > len(SHORT_OK), str(len(students[0].answer)))
    check("其余三份原样保留",
          all(s.answer.strip() == LONG_OK.strip() for s in students[1:]),
          str([len(s.answer) for s in students[1:]]))


def test_good_answer_untouched():
    """合格的答案不会被无谓改写（省额度）。"""
    print("\n[合格答案不动它]")
    stub, students = run_flow()
    check("没有触发改写", stub.fix_prompts == [], str(len(stub.fix_prompts)))
    check("答案原样保留", students[0].answer.strip() == GOOD.strip(),
          students[0].answer)
    check("学生顺序与设计档位一致", students[0].ability == "中等")


def test_design_score_is_expectation():
    """设计分现在是按档位估的"预期分"，不超过题面满分。"""
    print("\n[设计分]")
    from core.planner import make_plans
    mp = make_plans(QUESTION, 40, {"优秀": 1.0}, blank_rate=0.0)
    check("设计分不超过题面满分",
          all(p.design_score <= QUESTION.max_score + 1e-6 for p in mp),
          str(max(p.design_score for p in mp)))
    check("优秀档预期分高于中等档（按目标得分率）",
          make_plans(QUESTION, 1, {"优秀": 1.0})[0].design_score
          > make_plans(QUESTION, 1, {"中等": 1.0})[0].design_score)


def main() -> None:
    print("=" * 46)
    print(" 批改模拟器 · 答卷生成链路自测（离线）")
    print("=" * 46)
    test_no_rubric_leak()
    test_role_prompt()
    test_direction_gate()
    test_length_gate()
    test_class_balance_gate()
    test_good_answer_untouched()
    test_design_score_is_expectation()
    print("\n" + "=" * 46)
    if FAILED:
        print(f" 有 {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        sys.exit(1)
    print(" 全部通过")


if __name__ == "__main__":
    main()
