"""以「网页模式」启动：电脑开着服务，手机浏览器扫码就能用。

跟双击 exe 的区别：
    exe            → 弹一个桌面窗口，只有电脑能用
    本文件（网页） → 浏览器打开，**同一 WiFi 下的手机也能用**

用法：双击根目录的「start_web.bat」，不要直接跑这个文件。

---
【手机首次打开慢的问题 · 已在这里修掉】
原来要等 1~2 分钟，有两个原因，都在这里处理了：

1. **引擎默认从 Google 的 CDN（gstatic.com）下载** —— 国内手机基本连不上，
   只能干等到超时。这一条最致命。`no_cdn=True` 让它改从本机取
   （本机局域网传输 0.1 秒就能下完）。
2. **加载画面只有一个转圈动画**，用户不知道在等什么。
   `_progress_page()` 会往 Flet 的 index.html 里塞一个带百分比、
   带阶段说明（"正在下载程序" → "正在启动界面" → "快好了"）的进度页。

另外网页引擎也换成了体积最小的 skwasm（3.4 MB，比默认 canvaskit 6.9 MB 少一半）。
电脑端双击 exe 走原生窗口，完全不受这些设置影响。
"""

from __future__ import annotations

import re
from pathlib import Path

import flet as ft

from app.main import main
from core import lan
from core import paths
from core.config import load_config, setup_logging

PORT = 8550

# 手机浏览器要"画"出界面，底层是个 WebAssembly 引擎：
#   canvaskit（默认）6.9 MB —— 渲染最稳，但手机首次要下载 + 编译，1~2 分钟
#   skwasm         3.4 MB —— 体积少一半，手机上明显快
# 电脑端双击 exe 走原生窗口，完全不受这个设置影响。
_RENDERERS = {
    "skwasm": "SKWASM",
    "sk_wasm": "SKWASM",
    "canvaskit": "CANVAS_KIT",
    "canvas_kit": "CANVAS_KIT",
}


def _renderer(cfg: dict):
    """按 config.yaml 里的 ui.web_renderer 选引擎；auto / 填错 → None（用默认）。"""
    mode = str((cfg.get("ui") or {}).get("web_renderer", "auto")).strip().lower()
    name = _RENDERERS.get(mode)
    if not name:
        return None
    return getattr(ft.WebRenderer, name, None)


# --------------------------------------------------------------------------
# 加载进度页
# --------------------------------------------------------------------------
# 为什么需要它：Flet 自带的加载画面只有一个转圈的小图，没有百分比、
# 也不说在干什么。手机首次打开要下载 + 编译网页引擎，1 分多钟里
# 用户完全不知道发生了什么，多半以为坏了又刷新，越刷越慢。
#
# 做法：把 Flet 的 index.html 复制一份，替换掉那个转圈 div，
# 换成"百分比进度条 + 阶段说明 + 常见问题提示"。
# ⚠ 只在网页模式用，桌面 exe 走原生窗口，不经过这个文件。

_STAGE_TEXT = [
    # (进度上限%, 这一刻在干什么)
    (18, "正在下载程序…"),
    (55, "正在准备界面引擎…"),
    (85, "正在启动界面…"),
    (99, "正在连接你的电脑…"),
    (100, "好了，正在打开…"),
]

# 等待时长：超过这个秒数就提示可能的原因
_HINT_AFTER = 25


def _progress_html() -> str:
    """生成替换用的加载页 HTML。"""
    rows = "\n".join(
        f'    {{at: {pct}, text: "{text}"}}' for pct, text in _STAGE_TEXT
    )
    return f"""  <div id="loading">
    <style>
      body {{
        inset: 0; overflow: hidden; margin: 0; padding: 0; position: fixed;
        font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
        background: #F4F7FC; color: #1A2230;
      }}
      #loading {{
        align-items: center; display: flex; justify-content: center;
        height: 100%; width: 100%;
      }}
      .box {{
        background: #fff; border-radius: 16px; box-shadow: 0 6px 28px rgba(14,63,140,.10);
        padding: 30px 26px; max-width: 400px; width: 86%;
      }}
      h1 {{ font-size: 19px; margin: 0 0 4px; color: #0E3F8C; }}
      .sub {{ font-size: 13px; color: #6B7787; margin: 0 0 20px; }}
      .track {{
        background: #E3EAF6; border-radius: 999px; height: 11px; overflow: hidden;
      }}
      .bar {{
        background: linear-gradient(90deg,#1E4FA8,#4C8BE0);
        border-radius: 999px; height: 100%; width: 0%;
        transition: width .45s ease;
      }}
      .row {{
        display: flex; justify-content: space-between; align-items: baseline;
        margin-top: 10px; font-size: 13px; color: #4A5568;
      }}
      .pct {{ font-size: 20px; font-weight: 700; color: #1E4FA8; }}
      .tip {{
        margin-top: 18px; padding-top: 14px; border-top: 1px solid #EDF1F8;
        font-size: 12px; color: #8B97A8; line-height: 1.75;
      }}
      .tip b {{ color: #4A5568; }}
      .spinner {{
        width: 30px; height: 30px; margin: 0 auto 16px;
        border: 3px solid #E3EAF6; border-top-color: #1E4FA8;
        border-radius: 50%; animation: spin .9s linear infinite;
      }}
      @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
    <div class="box">
      <div class="spinner" id="spin"></div>
      <h1>批改模拟器</h1>
      <p class="sub">正在从你的电脑上加载</p>
      <div class="track"><div class="bar" id="bar"></div></div>
      <div class="row"><span id="stage">正在连接…</span><span class="pct" id="num">0%</span></div>
      <div class="tip" id="tip">
        <b>第一次打开会慢一些</b>（手机要下载并启动界面引擎），<br>
        之后就很快了。期间请不要关闭本页或刷新。
      </div>
    </div>
  </div>
  <script>
    (function () {{
      var STAGES = [
{rows}
      ];
      var bar = document.getElementById('bar');
      var num = document.getElementById('num');
      var stage = document.getElementById('stage');
      var tip = document.getElementById('tip');
      var spin = document.getElementById('spin');
      var pct = 0, done = false;

      // 真实进度拿不到（浏览器不告诉我们 wasm 编译到哪一步了），
      // 所以按时间平滑推到一个上限（96%），卡在 96% 等 Flutter 真正
      // 把界面画出来 —— 那一刻 Flet 会移除 #loading，下面的监听器负责收尾。
      var timer = setInterval(function () {{
        if (done) return;
        var ceiling = 96;
        pct = Math.min(ceiling, pct + Math.max(0.4, (ceiling - pct) * 0.035));
        paint();
      }}, 380);

      function paint() {{
        for (var i = 0; i < STAGES.length; i++) {{
          if (pct <= STAGES[i].at) {{ stage.textContent = STAGES[i].text; break; }}
        }}
        bar.style.width = pct.toFixed(0) + '%';
        num.textContent = pct.toFixed(0) + '%';
      }}

      // 30 秒还没好 → 给出可操作的排查提示
      setTimeout(function () {{
        if (done) return;
        tip.innerHTML =
          '<b>还在加载？</b>请检查：<br>' +
          '1) 手机和电脑连的是<b>同一个 WiFi</b><br>' +
          '2) 电脑上的<b>黑色窗口不要关</b>（关了就断开）<br>' +
          '3) 第一次弹防火墙时点<b>「允许访问」</b><br>' +
          '4) 别用微信里直接打开，点右上角<b>「在浏览器打开」</b><br>' +
          '实在不行就<b>下拉刷新本页</b>再试一次。';
        tip.style.color = '#B4761F';
      }}, {_HINT_AFTER} * 1000);

      // Flet 真正把界面画出来时，会插入 <flt-glass-pane> / <flutter-view>，
      // 并且是在 #loading 之后插入的。检测到它就收尾。
      var mo = new MutationObserver(function () {{
        if (done) return;
        var view = document.querySelector('flt-glass-pane, flutter-view, flt-scene-host');
        if (view) {{
          done = true;
          pct = 100;
          clearInterval(timer);
          var el = document.getElementById('loading');
          if (el) el.style.display = 'none';
        }}
      }});
      try {{ mo.observe(document.body, {{ childList: true, subtree: true }}); }} catch (e) {{}}

      // 兜底：4 分钟后无论怎样都放行，避免永远卡在进度页
      setTimeout(function () {{
        if (done) return;
        done = true;
        pct = 100; paint();
        stage.textContent = '正在打开…';
        var el = document.getElementById('loading');
        if (el) el.style.opacity = '0';
        setTimeout(function () {{ if (el) el.style.display = 'none'; }}, 400);
      }}, 240000);

      paint();
    }})();
  </script>
"""


def _web_root() -> Path:
    """Flet 自带的网页资源目录（里面就有 index.html）。"""
    try:
        import flet_web

        p = Path(flet_web.__file__).parent / "web"
        if (p / "index.html").exists():
            return p
    except Exception:
        pass
    return Path()


def install_progress_page(assets_dir: Path) -> bool:
    """把带百分比的加载页装到 assets/index.html，Flet 会优先用它。

    为什么放在 assets 就能生效：
    Flet 的 `FletStaticFiles` 在准备 web 根目录时，**先找用户 assets 目录里的
    index.html，找不到才用它自带的**（见 flet_web/fastapi/flet_static_files.py
    的 `copy_temp_web_file`）。所以只要往 assets 里放一份改过的 index.html，
    整个服务就都用它 —— 不用复制那 70 多 MB 的引擎资源。

    成功返回 True，失败返回 False（就用 Flet 自带的，只是少了进度提示）。
    """
    src = _web_root() / "index.html"
    if not src.exists():
        return False
    try:
        html = src.read_text(encoding="utf-8")
        start = html.find('<div id="loading">')
        boot = html.find('<script src="flutter_bootstrap.js"')
        if start < 0 or boot < 0 or start > boot:
            return False
        new_html = html[:start] + _progress_html() + html[boot:]
        # 自检：替换后不能丢掉 Flutter 的启动脚本
        if "flutter_bootstrap.js" not in new_html:
            return False
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "index.html").write_text(new_html, encoding="utf-8")
        return True
    except Exception:
        return False


def run(port: int = PORT) -> None:
    setup_logging()
    cfg = load_config()
    url = lan.access_url(port)
    assets = paths.resource("assets")

    # 装带百分比的加载页（放在 assets 里，Flet 会优先用它）
    has_progress = install_progress_page(assets)

    print()
    print("=" * 56)
    print("  批改模拟器 · 网页模式（手机也能用）")
    print("=" * 56)
    print(f"  这台电脑： http://localhost:{port}")
    print(f"  手机：     {url}")
    print()
    print("  · 手机要和电脑连同一个 WiFi")
    print("  · 第一次可能弹出防火墙提示，选「允许」")
    print("  · 浏览器打开后，点右上角「手机访问」可以看二维码")
    print("  · 关掉这个黑窗口就等于停止服务，手机会连不上")

    r = _renderer(cfg)
    if r is not None:
        print("  · 界面引擎：轻量版（skwasm，约 3.4 MB）")
    else:
        mode = str((cfg.get("ui") or {}).get("web_renderer", "auto")).strip().lower()
        if mode != "auto":
            print(f"  · [提示] config.yaml 里 web_renderer=\"{mode}\" 认不出，"
                  "已按默认启动（可填 auto / skwasm / canvaskit）")
    print("  · 界面资源全部从这台电脑直接取（不走国外 CDN，手机能连）")
    print("  · 手机首次打开会显示百分比进度；之后就很快了")
    if not has_progress:
        print("  · [提示] 加载进度页没装上（不影响使用，只是少了进度条）")
    print("=" * 56)
    print()

    # host 必须是 0.0.0.0：只监听 127.0.0.1 的话手机连不上
    # no_cdn=True 是关键：不加的话界面引擎会去 gstatic.com（Google 的 CDN）
    # 下载，国内手机基本连不上，只能干等到超时 —— 那就是之前"等一两分钟"的主因。
    kw = {
        "view": ft.AppView.WEB_BROWSER,
        "host": "0.0.0.0",
        "port": port,
        "assets_dir": str(assets),
        "no_cdn": True,
    }
    if r is not None:
        kw["web_renderer"] = r
    ft.run(main, **kw)


if __name__ == "__main__":
    run()
