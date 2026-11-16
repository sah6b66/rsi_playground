"""Anthropic 兼容真实 provider handler（S11，DD-02 §5.1 真实接线）.

经 LLMClient 注入的 Handler 协议调用 Anthropic 兼容 ``/v1/messages``
（官方 API 或 bigmodel 等兼容网关）。鉴权同时携带 ``x-api-key`` 与
``Authorization: Bearer``——兼容网关两种取法都接受。prompt 文本由
LLMClient 经 ``vars["prompt"]`` 传入（PromptLibrary 渲染产物）。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import httpx

from rsi.log import get_logger

__all__ = [
    "AnthropicCompatHandler",
    "RetryableProviderError",
    "ProviderHTTPError",
    "ProviderChain",
]

logger = get_logger(__name__)


class RetryableProviderError(RuntimeError):
    """传输层重试耗尽（429/5xx/网络/超时）."""


class ProviderHTTPError(RuntimeError):
    """非重试类 provider HTTP 错误（4xx 等）."""


class ProviderChain:
    """主备 handler 链：主 RetryableProviderError → 切备；其余异常直抛（DD-02 §5.1）."""

    def __init__(self, primary, backup=None):
        self._primary = primary
        self._backup = backup

    def __call__(
        self,
        component: str,
        prompt_name: str,
        vars: dict[str, str],
        *,
        temperature: float = 0.0,
    ) -> str:
        try:
            return self._primary(
                component, prompt_name, vars, temperature=temperature
            )
        except RetryableProviderError:
            if self._backup is None:
                raise
            return self._backup(
                component, prompt_name, vars, temperature=temperature
            )


class AnthropicCompatHandler:
    """Callable LLM handler posting to an Anthropic-compatible /v1/messages."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "glm-5.1",
        timeout_s: float = 120.0,
        max_tokens: int = 4096,
        retry_max: int = 3,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.retry_max = retry_max

    def __call__(
        self,
        component: str,
        prompt_name: str,
        vars: dict[str, str],
        *,
        temperature: float = 0.0,
    ) -> str:
        prompt = str(vars.get("prompt") or json.dumps(vars, ensure_ascii=False))
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        last_retryable: Exception | None = None
        for attempt in range(self.retry_max):
            try:
                resp = httpx.post(
                    f"{self.base_url}/v1/messages",
                    json=body,
                    headers=headers,
                    timeout=self.timeout_s,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_retryable = exc
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            if resp.status_code == 200:
                break
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_retryable = ProviderHTTPError(
                    f"llm provider http {resp.status_code}: {resp.text[:300]}"
                )
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            raise ProviderHTTPError(
                f"llm provider http {resp.status_code}: {resp.text[:300]}"
            )
        else:
            raise RetryableProviderError(
                f"llm provider unreachable after {self.retry_max} attempts: "
                f"{last_retryable}"
            )
        data = resp.json()
        text = "".join(
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        )
        if not text:
            raise RuntimeError("llm provider returned no text blocks")
        usage = data.get("usage") or {}
        logger.debug(
            "llm.provider_call",
            component=component,
            prompt=prompt_name,
            model=self.model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        return text
