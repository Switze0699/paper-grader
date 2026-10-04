"""界面构建自测：不弹窗，直接把每个页面的组件树搭一遍。

运行方式：
    .venv\\Scripts\\python.exe tests\\test_ui.py

作用：提前发现"某个控件写法不对"这类只有打开页面才会暴露的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.screens import grading, history, mobile, report, review, setup  # noqa: E402
from core.config import load_config  # noqa: E402
from core.models import Question, RubricPoint, Student  # noqa: E402
from storage import repository as repo  # noqa: E402

FAILED = []


class StubApp:
    """假的 App：只负责接收控件，不真的开窗口。"""

    def __init__(self, paper: dict | None = None):
        self.cfg = load_config()
        self.api_key = "test-key"
        self.rendered = None
        self.navigated = []
        if paper:
            self.state = {
                "question": paper["question"],
                "students": paper["students"],
                "ai": paper["ai"],
                "teacher": paper["teacher"],
                "idx": 0,
                "view_seq": paper["students"][0].seq if paper["students"] else None,
                "paper_id": paper["paper_id"],
                "repeats": paper["repeats"],
            }
        else:
            self.state = {
                "question": None, "students": [], "ai": {}, "teacher": {},
                "idx": 0, "view_seq": None, "paper_id": None, "repeats": 3,
            }

    def render(self, controls):
        self.rendered = controls

    def show_error(self, message, on_retry=None):
        raise AssertionError("不该出错：" + message)

    def go_setup(self):
        self.navigated.append("setup")

    def go_grading(self):
        self.navigated.append("grading")

    def go_report(self):
        self.navigated.append("report")

    def go_review(self, seq):
        self.state["view_seq"] = seq
        self.navigated.append("review")

    def go_history(self):
        self.navigated.append("history")


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  通过  {name}")
    else:
        print(f"  失败  {name} {extra}")
        FAILED.append(name)


def _to_text(ctrl, depth: int = 0) -> str:
    """把 Flet 控件树里所有文字抠出来，用来断言"页面上出现了某句话"。

    只认 Text / label 这类带 value 字符串的控件，够用且不依赖 Flet 内部结构。
    """
    if ctrl is None or depth > 12:
        return ""
    out = ""
    v = getattr(ctrl, "value", None)
    if isinstance(v, str):
        out += v + " "
    for attr in ("controls", "content"):
        sub = getattr(ctrl, attr, None)
        if isinstance(sub, list):
            for c in sub:
                out += _to_text(c, depth + 1)
        elif sub is not None:
            out += _to_text(sub, depth + 1)
    return out


def _fake_paper() -> dict:
    q = Question(
        subject="地理", topic="河流水文", stem="分析该河流的水文特征。",
        max_score=6,
        points=[
            RubricPoint(seq=1, text="流量大：降水丰富", score=2),
            RubricPoint(seq=2, text="水位季节变化大", score=2),
            RubricPoint(seq=3, text="含沙量小：植被覆盖率提高", score=2),
        ],
    )
    from core.models import AiScore, PointResult

    students = [
        Student(seq=1, ability="中等", answer="该河流量较大，水位季节变化明显。"),
        Student(seq=2, ability="优秀", answer="流量大；水位季节变化大；含沙量小。"),
    ]
    ai = {
        1: AiScore(1, 3.0, 1.0, 0.0, [
            PointResult(1, "流量大：降水丰富", 2, 2, 1.0, "写到了流量大", "流量较大"),
            PointResult(2, "水位季节变化大", 2, 1, 0.67, "没写夏冬对比", "水位季节变化明显"),
            PointResult(3, "含沙量小", 2, 0, 1.0, "没提到含沙量", ""),
        ], comment="基本掌握"),
        2: AiScore(2, 6.0, 1.0, 0.0, [
            PointResult(1, "流量大：降水丰富", 2, 2, 1.0, "正确", "流量大"),
            PointResult(2, "水位季节变化大", 2, 2, 1.0, "正确", "水位季节变化大"),
            PointResult(3, "含沙量小", 2, 2, 1.0, "正确", "含沙量小"),
        ], comment="掌握扎实"),
    }
    return {
        "paper_id": 0, "question": q, "students": students, "ai": ai,
        "teacher": {1: 4, 2: 6}, "created_at": "2026-01-01 00:00:00", "repeats": 3,
    }


def main() -> None:
    print("=" * 46)
    print(" 批改模拟器 · 界面构建自测")
    print("=" * 46)

    print("\n[出题页]")
    app = StubApp()
    setup.render(app)
    check("无题目时能显示开始卡片", app.rendered is not None and len(app.rendered) >= 1)

    app.state["question"] = _fake_paper()["question"]
    setup.render(app)
    check("有题目时能显示细则编辑区",
          app.rendered is not None and len(app.rendered) >= 4,
          str(len(app.rendered) if app.rendered else 0))

    print("\n[批改页]")
    app = StubApp(_fake_paper())
    grading.render(app)
    check("能构建批改页", app.rendered is not None and len(app.rendered) >= 4)

    print("\n[报告页]")
    report.render(app)
    check("能构建报告页", app.rendered is not None and len(app.rendered) >= 4)

    # ---- 耗时统计（2026-10-04 新增）----
    from core.timing import (Stopwatch, fmt_duration, get_api_calls,
                             note_api_call, reset_api_calls)
    check("时长会按大小自动换单位（秒/分/小时）",
          fmt_duration(0.8).endswith("秒") and fmt_duration(65) == "1 分 05 秒"
          and fmt_duration(3725).startswith("1 小时"), 
          f"{fmt_duration(0.8)} / {fmt_duration(65)} / {fmt_duration(3725)}")
    check("没数据时显示破折号而不是 0",
          fmt_duration(None) == "—", fmt_duration(None))
    sw = Stopwatch()
    sw.mark("answer"); sw.mark("grade")
    d = sw.as_dict()
    check("Stopwatch 能分别记出每一步的耗时",
          "answer" in d and "grade" in d and d["total"] >= d["answer"],
          str({k: round(v, 3) for k, v in d.items()}))
    sw.mark("answer")
    check("同一步重复 mark 是累加（重试场景），不是覆盖",
          round(sw.steps["answer"], 3) >= round(d["answer"], 3))
    reset_api_calls(); note_api_call(); note_api_call(2)
    check("API 调用计数能累加", get_api_calls() == 3, str(get_api_calls()))
    reset_api_calls()

    from storage import db as _db
    _cols = {r[1] for r in _db.get_conn().execute("PRAGMA table_info(papers)")}
    check("papers 表有耗时字段（老库会被自动补列）",
          {"t_question", "t_answer", "t_grade", "t_total", "api_calls"} <= _cols,
          str(sorted(_cols)))
    check("repository 有 save_timing / load_timing / timing_history",
          all(hasattr(repo, f) for f in
              ("save_timing", "load_timing", "timing_history")))

    # 报告页在**有**耗时数据时能渲染出耗时卡片
    _saved = dict(app.state.get("timing") or {})
    app.state["timing"] = {"question": 12.3, "answer": 65.0,
                           "grade": 150.2, "total": 228.0}
    app.state["api_calls"] = 32
    _card = report._timing_card(app)
    check("有耗时数据时能渲染出「本次批改耗时」卡片",
          _card is not None, str(_card))
    check("耗时卡片里带上了三个步骤和合计",
          _card is not None and "3 分 48 秒" in _to_text(_card),
          _to_text(_card)[:120] if _card is not None else "")
    app.state["timing"] = None
    app.state["api_calls"] = None
    check("没有耗时数据时不显示耗时卡（不给用户看一片破折号）",
          report._timing_card(app) is None)

    # ⚠ 键名一致性：界面/内存里用 question/answer/grade/total（不带 t_），
    #   数据库列名才带 t_ 前缀。两边混用会让耗时卡片默默变成 None。
    from core.timing import STEP_LABELS as _LBL
    check("耗时键名统一用不带 t_ 前缀的那套",
          all(k in _LBL for k in ("question", "answer", "grade", "total"))
          and not any(k.startswith("t_") for k in _LBL),
          str(sorted(_LBL)))
    _src = Path(report.__file__).read_text(encoding="utf-8")
    check("从数据库读耗时时会转成不带 t_ 的键名",
          '"question": row.get("t_question")' in _src)
    app.state["timing"] = _saved or None

    print("\n[回溯页]")
    review.render(app)
    check("能构建单份回溯页", app.rendered is not None and len(app.rendered) >= 4)

    print("\n[历史页]")
    history.render(app)
    check("能构建历史页（含真实存档）",
          app.rendered is not None and len(app.rendered) >= 2)

    print("\n[密钥输入页]")
    # 安卓上没有 .env 文件也没法用记事本改，必须能在界面上直接填密钥
    from core.config import save_api_key, _key_file
    import os as _os
    app.api_key = ""                       # 模拟"没配密钥"
    app.state.pop("_key_field", None)
    setup.render(app)
    key_box = app.state.get("_key_field")
    check("没有密钥时，首页给出一个可填的密钥输入框",
          key_box is not None and bool(getattr(key_box, "password", False)))
    save_api_key("test-key-for-ui")
    check("密钥能从界面保存下来并读回",
          save_api_key("test-key-for-ui") is None
          and _os.path.exists(_key_file()))
    _os.remove(_key_file())
    from core.config import get_api_key
    app.api_key = get_api_key(app.cfg)
    app.state.pop("_key_field", None)

    print("\n[手机访问页]")
    mobile.render(app)
    check("能构建手机访问页", app.rendered is not None and len(app.rendered) >= 4)
    from core import lan
    ip = lan.lan_ip()
    check("能查出局域网 IP（不是 127.0.0.1）",
          ip != "127.0.0.1" and ip.count(".") == 3, ip)
    check("访问网址里带上了 IP 和端口",
          ip in lan.access_url(8550) and "8550" in lan.access_url(8550),
          lan.access_url(8550))
    check("二维码能生成（data URL）",
          lan.qr_data_url("http://x").startswith("data:image/png;base64,"))

    # ---- 网页版"手机首次打开慢"的两个修复不许被改回去 ----
    from pathlib import Path as _P
    from app.server import _progress_html, _web_root
    from app import server as _srv

    check("加载页换成了带百分比的版本（不再只有一个转圈图）",
          'class="bar"' in _progress_html()
          and "loading-animation.png" not in _progress_html()
          and "正在从你的电脑上加载" in _progress_html())
    # no_cdn=True 是关键：不加的话引擎会去 gstatic.com（Google CDN），
    # 国内手机连不上，只能干等到超时 —— 那就是"等一两分钟"的主因。
    run_src = _P(_srv.__file__).read_text(encoding="utf-8")
    check("启动时必须传 no_cdn=True（不让引擎走国外 CDN）",
          '"no_cdn": True' in run_src or "'no_cdn': True" in run_src)
    check("默认用轻量引擎 skwasm（3.4MB，比默认 canvaskit 少一半）",
          "skwasm" in run_src.lower())
    check("能定位到 Flet 自带的 index.html（进度页靠它改）",
          (_web_root() / "index.html").exists(), str(_web_root()))

    # 实际装一次，确认替换逻辑有效（会用 Flet 原版重装回 assets/index.html）
    from core import paths as _paths
    _ok = _srv.install_progress_page(_paths.resource("assets"))
    _idx = _paths.resource("assets") / "index.html"
    check("能把进度页装到 assets/index.html（Flet 会优先用它）",
          _ok and _idx.exists()
          and 'class="bar"' in _idx.read_text(encoding="utf-8"),
          str(_idx))
    check("装过的 index.html 仍留着 Flet 配置占位符",
          "<!-- fletAppConfig -->" in _idx.read_text(encoding="utf-8"))

    papers = repo.list_papers()
    print(f"\n  （数据库里现有 {len(papers)} 条存档记录）")

    print("\n" + "=" * 46)
    if FAILED:
        print(f" 有 {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        sys.exit(1)
    print(" 全部通过")


if __name__ == "__main__":
    main()
