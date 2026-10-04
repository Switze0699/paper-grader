"""智谱接口的离线自测：不发真实请求，用假接口检查参数和错误处理对不对。

运行方式：.venv\\Scripts\\python.exe tests\\test_llm_zhipu.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from services.llm import LLMClient, LLMError  # noqa: E402

CFG = {
    "ai": {
        "provider": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.5-air",
        "timeout_seconds": 10,
        "max_retries": 2,
        "temperature_max": 1.0,
        "max_tokens": 8192,
        "json_mode": True,
        "thinking": "disabled",
        "greedy_when_zero": True,
    }
}


def make_client(handler, cfg=None, key="test-key"):
    c = LLMClient(cfg or CFG, key)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def ok(content: str, finish="stop"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content},
                           "finish_reason": finish}]},
    )


def test_payload_and_clamp():
    """检查请求地址、鉴权、温度压缩、贪心解码等参数。"""
    bodies = []
    seen = {}

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return ok('{"ok": true}')

    async def run():
        c = make_client(handler)
        await c.chat_json("sys", "user", temperature=1.5)  # 超出 1，应被压到 1.0
        await c.chat_json("sys", "user", temperature=0.0)  # 阅卷温度，应加 do_sample
        await c.aclose()

    asyncio.run(run())

    assert seen["url"].endswith("/chat/completions"), seen["url"]
    assert seen["url"].startswith("https://open.bigmodel.cn/api/paas/v4"), seen["url"]
    assert seen["auth"] == "Bearer test-key", seen["auth"]

    hot, cold = bodies[0], bodies[1]
    assert hot["model"] == "glm-4.5-air"
    assert hot["temperature"] == 1.0, hot["temperature"]
    assert hot["max_tokens"] == 8192
    assert hot["thinking"] == {"type": "disabled"}
    assert hot["response_format"] == {"type": "json_object"}
    assert "do_sample" not in hot, hot

    assert cold["temperature"] == 0.0
    assert cold["do_sample"] is False, "温度为 0 时应启用贪心解码"
    body = cold

    # 非 0 温度不应加 do_sample
    seen2 = {}

    def handler2(request: httpx.Request):
        seen2["body"] = json.loads(request.content)
        return ok('{"ok": true}')

    async def run2():
        c = make_client(handler2)
        await c.chat_json("sys", "user", temperature=0.6)
        await c.aclose()

    asyncio.run(run2())
    assert "do_sample" not in seen2["body"], seen2["body"]
    assert seen2["body"]["temperature"] == 0.6
    print("OK  请求参数正确（地址/鉴权/温度压缩/贪心解码/thinking）")


def test_drop_unsupported_param():
    """服务端不认识 response_format 时，应自动去掉并重试成功。"""
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported response_format"}})
        return ok('{"ok": true}')

    async def run():
        c = make_client(handler)
        data = await c.chat_json("sys", "user", temperature=0.0)
        await c.aclose()
        return data

    assert asyncio.run(run()) == {"ok": True}
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1], calls[1]
    print("OK  遇到不支持的参数会自动精简后重试")


def test_friendly_errors():
    """密钥错误要立刻报人话，不要傻等重试。"""
    calls = []

    def handler(request: httpx.Request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    async def run():
        c = make_client(handler)
        try:
            await c.chat_json("sys", "user", temperature=0.0)
        except LLMError as e:
            return str(e)
        finally:
            await c.aclose()
        return ""

    msg = asyncio.run(run())
    assert "ZHIPU_API_KEY" in msg, msg
    assert len(calls) == 1, f"401 不该重试，实际请求了 {len(calls)} 次"
    print("OK  401 密钥错误 → 直接给出中文提示，不空转重试")

    async def run_nokey():
        c = make_client(handler, key="")
        try:
            await c.chat_json("sys", "user")
        except LLMError as e:
            return str(e)
        return ""

    assert "ZHIPU_API_KEY" in asyncio.run(run_nokey())
    print("OK  没填密钥 → 提示去 .env 填 ZHIPU_API_KEY")


def test_thinking_only_output():
    """只返回思维链、没有正文时，要给出明确原因。"""

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "reasoning_content": "让我想想…"},
                               "finish_reason": "stop"}]},
        )

    async def run():
        c = make_client(handler)
        try:
            await c.chat_json("sys", "user", temperature=0.0)
        except LLMError as e:
            return str(e)
        finally:
            await c.aclose()
        return ""

    msg = asyncio.run(run())
    assert "思维链" in msg, msg
    print("OK  只有思维链没有正文 → 提示关闭 thinking 或调大长度")


def test_truncated_but_usable():
    """被长度截断但仍是可解析的 JSON 时，正常返回（只记警告）。"""

    def handler(request: httpx.Request):
        return ok('{"points": [{"index": 1, "level": 2}]}', finish="length")

    async def run():
        c = make_client(handler)
        data = await c.chat_json("sys", "user", temperature=0.0)
        await c.aclose()
        return data

    assert asyncio.run(run()) == {"points": [{"index": 1, "level": 2}]}
    print("OK  输出被截断但可解析 → 正常返回并告警")


def test_env_override():
    """.env 里可以覆盖地址和模型。"""
    import os

    from core.config import load_config

    os.environ["AI_MODEL"] = "glm-4.6"
    try:
        cfg = load_config()
        assert cfg["ai"]["model"] == "glm-4.6", cfg["ai"]["model"]
        assert cfg["ai"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert cfg["ai"]["api_key_env"] == "ZHIPU_API_KEY"
    finally:
        os.environ.pop("AI_MODEL", None)
    print("OK  配置读取正常，.env 可覆盖模型")


if __name__ == "__main__":
    test_payload_and_clamp()
    test_drop_unsupported_param()
    test_friendly_errors()
    test_thinking_only_output()
    test_truncated_but_usable()
    test_env_override()
    print("\n全部通过：智谱接口适配没问题。")
