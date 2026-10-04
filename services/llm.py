"""AI 接口封装：发请求、重试、解析 JSON。

当前对接的是「智谱 GLM」（OpenAI 兼容格式，地址 https://open.bigmodel.cn/api/paas/v4）。
所有网络相关的麻烦事都关在这个文件里，其它模块只管调用 chat_json()。

需要知道的几件事：
· 智谱的 temperature 只能在 0~1 之间，这里会自动把超出的值压回范围内
· 温度为 0 时会额外传 do_sample=false，让模型走"贪心解码"，彻底不随机（阅卷稳定性靠它）
· 如果服务端不认识某个参数（json 模式、thinking 等），会自动去掉这个参数重试一次
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

from core.timing import note_api_call

log = logging.getLogger(__name__)

# 智谱开放平台（国内版）的 OpenAI 兼容地址
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# 出错时给用户看的人话解释
_HTTP_TIPS = {
    401: "密钥无效或没生效：检查 .env 里的 ZHIPU_API_KEY 有没有填对（不要有多余空格或引号），"
         "以及 config.yaml 里的 api_key_env 是不是 ZHIPU_API_KEY。",
    403: "这个账号没有该模型的调用权限，或账号被限制。",
    402: "账户余额不足，请到智谱开放平台（open.bigmodel.cn）充值后重试。",
    404: "接口地址不对：config.yaml 里的 ai.base_url 应为 " + ZHIPU_BASE_URL,
    429: "请求太频繁被限流了。把 config.yaml 里的并发数调小一点，稍后再试。",
    408: "请求超时，网络可能不通畅，稍后再试。",
}


class LLMError(RuntimeError):
    """AI 调用彻底失败。"""


def _repair_quotes(s: str) -> str:
    """把 JSON 字符串内部"没转义的英文双引号"补上转义。

    实测踩过的坑：让 AI 在 reason 里写"缺的是哪个核心定语"之后，
    它会顺手把那个词用英文双引号括起来：

        "reason": "缺少"地势相对平坦开阔"的核心定语，只笼统说地形条件有利于农业生产"

    字符串从中间被截断，整个 JSON 解析失败、白白重试 3 次、那份答卷判 0 分。
    这里做一个保守修复：扫一遍，遇到引号时看它后面第一个非空白字符——
    是 `,` `:` `}` `]` 就当成正常的字符串结束符，否则当成内容里的引号补上反斜杠。

    只在 `json.loads` 失败之后才调用，所以不会破坏本来就合法的 JSON。
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if ch == "\\":  # 已经是转义序列，原样复制两个字符
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in ",:}]":
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')  # 这是字符串内容里的引号，补个反斜杠
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_json(text: str) -> Dict[str, Any]:
    """从 AI 的回复里抠出 JSON，容忍 ```json 围栏和前后废话。"""
    if not text:
        raise ValueError("AI 返回了空内容")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            s = s[start : end + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 再试一次：修掉"字符串里混进英文双引号"这种最常见的坏法
    try:
        return json.loads(_repair_quotes(s))
    except json.JSONDecodeError as e:
        raise ValueError(f"AI 返回的不是合法 JSON：{e}\n原文前 200 字：{text[:200]}") from e


def explain_http(status: int, body: str) -> str:
    """把 HTTP 错误码翻译成人话。"""
    tip = _HTTP_TIPS.get(status)
    if status >= 500:
        tip = "智谱服务器繁忙或暂时不可用，稍等一会儿再试。"
    detail = (body or "").strip().replace("\n", " ")[:200]
    if tip:
        return f"HTTP {status}：{tip}\n接口原文：{detail}"
    return f"HTTP {status}：{detail}"


class LLMClient:
    """一次配置，多处复用。构造时读 config.yaml 的 ai 段。"""

    def __init__(self, cfg: dict, api_key: str):
        ai = cfg.get("ai", {})
        self.provider = str(ai.get("provider", "zhipu")).lower()
        self.base_url = str(ai.get("base_url") or ZHIPU_BASE_URL).rstrip("/")
        self.model = str(ai.get("model", "glm-4.5-air"))
        self.api_key = (api_key or "").strip()
        self.timeout = float(ai.get("timeout_seconds", 120))
        self.max_retries = int(ai.get("max_retries", 3))

        # ---- 智谱专属参数（config.yaml 可调）----
        # 温度上限：智谱要求 0~1，超出部分自动压回
        self.temperature_max = float(ai.get("temperature_max", 1.0))
        # 单次回复最大 token 数：一次要写 8 份答卷，给足余量
        self.max_tokens = int(ai.get("max_tokens", 8192))
        # 是否强制 JSON 输出：部分模型不支持，默认关（靠提示词 + 本地兜底解析）
        self.json_mode = bool(ai.get("json_mode", False))
        # 思维链开关：disabled=关闭（更快更稳，推荐）/ enabled=开启 / 留空=不传
        thinking = ai.get("thinking")
        thinking = str(thinking).strip().lower() if thinking else ""
        self.thinking = thinking if thinking in ("enabled", "disabled") else None
        # 温度为 0 时改用贪心解码，彻底锁死随机性（阅卷稳定性关键）
        self.greedy_when_zero = bool(ai.get("greedy_when_zero", True))

        self._verify = True
        self._client: Optional[httpx.AsyncClient] = None
        # 被服务端拒绝过、以后都不再发送的参数名
        self._dropped: set[str] = set()

    # ---------- 基础设施 ----------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, verify=self._verify
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------- 请求组装 ----------

    def _clamp_temperature(self, temperature: float) -> float:
        try:
            t = float(temperature)
        except (TypeError, ValueError):
            t = 0.0
        t = max(0.0, min(t, self.temperature_max))
        return round(t, 2)  # 智谱要求最多两位小数

    def _build_payload(
        self, system: str, user: str, temperature: float, want_json: bool
    ) -> Dict[str, Any]:
        t = self._clamp_temperature(temperature)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": t,
        }
        if "max_tokens" not in self._dropped:
            payload["max_tokens"] = self.max_tokens
        if want_json and self.json_mode and "response_format" not in self._dropped:
            payload["response_format"] = {"type": "json_object"}
        if self.thinking and "thinking" not in self._dropped:
            payload["thinking"] = {"type": self.thinking}
        if t <= 0 and self.greedy_when_zero and "do_sample" not in self._dropped:
            payload["do_sample"] = False
        return payload

    def _drop_unsupported_param(self, body: str, payload: Dict[str, Any]) -> bool:
        """服务端报"不认识某个参数"时，把这个参数永久去掉，返回 True 表示需要重试。"""
        low = (body or "").lower()
        for name, keywords in (
            ("response_format", ("response_format", "json_object")),
            ("thinking", ("thinking",)),
            ("do_sample", ("do_sample",)),
        ):
            if name in payload and name not in self._dropped:
                if any(k in low for k in keywords):
                    self._dropped.add(name)
                    log.warning("接口不支持参数 %s，已自动去掉后重试", name)
                    return True
        return False

    # ---------- 结果解析 ----------

    @staticmethod
    def _parse_response(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise LLMError(f"AI 返回内容格式异常：{str(data)[:200]}")
        if "choices" not in data:
            err = data.get("error")
            raise LLMError(f"AI 接口报错：{str(err)[:300] if err else str(data)[:300]}")

        choices = data["choices"] or []
        if not choices:
            raise LLMError("AI 返回内容为空（choices 为空）")
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content")

        if not (content and str(content).strip()):
            if msg.get("reasoning_content"):
                raise LLMError(
                    "AI 只返回了思维链、没有正式答案。请把 config.yaml 里的 "
                    "ai.thinking 设为 disabled，或把 ai.max_tokens 调大。"
                )
            raise LLMError("AI 返回了空内容，请稍后重试。")

        if choice.get("finish_reason") == "length":
            log.warning("AI 输出被长度上限截断，结果可能不完整，"
                        "可把 config.yaml 里的 ai.max_tokens 调大")

        return extract_json(str(content))

    # ---------- 主入口 ----------

    async def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        response_format: bool = True,
    ) -> Dict[str, Any]:
        """调用 AI 并解析成 dict，失败自动重试。"""
        if not self.api_key:
            raise LLMError(
                "还没有配置 API Key。请在 .env 里填写 ZHIPU_API_KEY=你的密钥，"
                "然后重新启动程序。"
            )
        if not self.api_key.isascii():
            raise LLMError(
                "密钥里有中文或全角字符，通常是 .env 里那一行粘错了内容。"
                "请检查 ZHIPU_API_KEY 后面是不是只粘了一串英文和数字。"
            )

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            payload = self._build_payload(system, user, temperature, response_format)
            try:
                client = await self._ensure_client()
                # 每次真正发出的请求都记一笔（重试也算，用户关心的是额度）
                note_api_call()
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.ConnectError as e:
                # 常见于公司网络/杀毒软件拦截证书，自动降级一次
                if self._verify:
                    log.warning("SSL 连接失败，改为不校验证书重试：%s", e)
                    self._verify = False
                    self._client = None
                last_err = e
            except UnicodeError as e:
                raise LLMError(
                    "密钥里有无法发送的字符，通常是 .env 里粘错了内容。"
                    "请检查 ZHIPU_API_KEY 后面是不是只粘了一串英文和数字。"
                ) from e
            except Exception as e:  # noqa: BLE001
                last_err = e
            else:
                if r.status_code == 200:
                    try:
                        return self._parse_response(r.json())
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                else:
                    body = r.text or ""
                    err = LLMError(explain_http(r.status_code, body))
                    if r.status_code == 400 and self._drop_unsupported_param(
                        body, payload
                    ):
                        last_err = err  # 去掉不支持的参数，下一轮立刻重试
                    elif r.status_code in (400, 401, 403):
                        raise err  # 密钥/权限/参数问题，重试也没用
                    else:
                        last_err = err
                log.warning("AI 调用第 %s 次失败：%s", attempt, last_err)

            if attempt < self.max_retries:
                await asyncio.sleep(1.5 * attempt)

        raise LLMError(f"AI 调用失败（已重试 {self.max_retries} 次）：{last_err}")

    async def ping(self) -> str:
        """自检用：发一条最小请求，确认密钥和地址都能通。"""
        data = await self.chat_json(
            "你只回答 JSON。",
            '请返回 {"ok": true}',
            temperature=0.0,
        )
        return f"连通正常，模型 {self.model} 返回：{str(data)[:80]}"
