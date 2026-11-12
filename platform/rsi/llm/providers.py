"""Anthropic 兼容真实 provider handler（S11，DD-02 §5.1 真实接线）.

经 LLMClient 注入的 Handler 协议调用 Anthropic 兼容 ``/v1/messages``
（官方 API 或 bigmodel 等兼容网关）。鉴权同时携带 ``x-api-key`` 与
``Authorization: Bearer``——兼容网关两种取法都接受。prompt 文本由
LLMClient 经 ``vars["prompt"]`` 传入（PromptLibrary 渲染产物）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rsi.log import get_logger

__all__ = ["AnthropicCompatHandler"]

logger = get_logger(__name__)


class AnthropicCompatHandler:
    """Callable LLM handler posting to an Anthropic-compatible /v1/messages."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "glm-5.1",
        timeout_s: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

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
        resp = httpx.post(
            f"{self.base_url}/v1/messages",
            json=body,
            headers=headers,
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"llm provider http {resp.status_code}: {resp.text[:300]}"
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
