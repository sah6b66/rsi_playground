"""TU for LLMClient (offline mock) — DD-02 §4/§5.1 subset: cassette dict,
injectable handler, semaphore, schema validation with retry<=2."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from rsi.core.errors import LLMOutputInvalid
from rsi.llm.client import LLMClient


class _Out(BaseModel):
    verdict: str
    confidence: float


def _make(handler: Any = None) -> LLMClient:
    return LLMClient(handler=handler)


@pytest.mark.asyncio
async def test_complete_returns_mock_response() -> None:
    client = _make(lambda c, p, v: "hello")
    resp = await client.complete("judge", "classify", {"x": "1"})
    assert resp.content == "hello"
    assert resp.parsed is None
    assert resp.model == "mock"
    assert resp.prompt_version == "v1"
    assert resp.usage.estimated is True  # B1-7: 纯 str handler 走估算


@pytest.mark.asyncio
async def test_cassette_hit_skips_handler() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(component: str, prompt_name: str, vars: dict) -> str:
        calls.append((component, prompt_name, vars))
        return "fresh"

    client = _make(handler)
    first = await client.complete("judge", "classify", {"x": "1"})
    second = await client.complete("judge", "classify", {"x": "1"})
    assert first is second or second.content == "hello" or second.content == "fresh"
    # cached: handler invoked exactly once
    assert len(calls) == 1
    # cached response returned verbatim
    assert second.content == first.content


@pytest.mark.asyncio
async def test_cassette_key_varies_by_vars() -> None:
    seen: list[dict] = []

    def handler(component: str, prompt_name: str, vars: dict) -> str:
        seen.append(dict(vars))
        return "out"

    client = _make(handler)
    await client.complete("judge", "classify", {"x": "1"})
    await client.complete("judge", "classify", {"x": "2"})
    await client.complete("judge", "classify", {"x": "1"})
    assert seen == [{"x": "1"}, {"x": "2"}]


@pytest.mark.asyncio
async def test_schema_validation_parses() -> None:
    def handler(component: str, prompt_name: str, vars: dict) -> str:
        return '{"verdict": "pass", "confidence": 0.9}'

    client = _make(handler)
    resp = await client.complete("judge", "classify", {}, schema=_Out)
    assert isinstance(resp.parsed, _Out)
    assert resp.parsed.verdict == "pass"
    assert resp.content == '{"verdict": "pass", "confidence": 0.9}'


@pytest.mark.asyncio
async def test_schema_retry_recovers_with_error_feedback() -> None:
    outputs = ["not json", '{"verdict": "pass", "confidence": 0.5}']
    feedbacks: list[str | None] = []

    def handler(component: str, prompt_name: str, vars: dict) -> str:
        feedbacks.append(vars.get("validation_error"))
        return outputs.pop(0)

    client = _make(handler)
    resp = await client.complete("judge", "classify", {}, schema=_Out)
    assert isinstance(resp.parsed, _Out)
    assert len(feedbacks) == 2
    assert feedbacks[0] is None
    assert feedbacks[1] is not None and "validation" in feedbacks[1].lower()


@pytest.mark.asyncio
async def test_schema_retry_exhausted_raises() -> None:
    def handler(component: str, prompt_name: str, vars: dict) -> str:
        return "still not json"

    client = _make(handler)
    with pytest.raises(LLMOutputInvalid):
        await client.complete("judge", "classify", {}, schema=_Out)


@pytest.mark.asyncio
async def test_retry_limit_is_two_extra_attempts() -> None:
    attempts: list[int] = []

    def handler(component: str, prompt_name: str, vars: dict) -> str:
        attempts.append(1)
        return "garbage"

    client = _make(handler)
    with pytest.raises(LLMOutputInvalid):
        await client.complete("judge", "classify", {}, schema=_Out)
    assert len(attempts) == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency() -> None:
    active = 0
    peak = 0

    def handler(component: str, prompt_name: str, vars: dict) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        import time

        time.sleep(0.01)
        active -= 1
        return "ok"

    client = _make(handler)
    await asyncio.gather(
        *[client.complete("judge", "classify", {"i": str(i)}) for i in range(10)]
    )
    assert peak <= 4


@pytest.mark.asyncio
async def test_handler_exception_propagates_uncached() -> None:
    def handler(component: str, prompt_name: str, vars: dict) -> str:
        raise RuntimeError("boom")

    client = _make(handler)
    with pytest.raises(RuntimeError):
        await client.complete("judge", "classify", {})
    assert client.cassette == {}


@pytest.mark.asyncio
async def test_complete_uses_real_usage_from_handler() -> None:
    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "plain text", {"input_tokens": 11, "output_tokens": 22}

    client = _make(handler)
    resp = await client.complete("judge", "root_cause", {"q": "x"})
    assert resp.usage.input_tokens == 11
    assert resp.usage.output_tokens == 22
    assert resp.usage.estimated is False


@pytest.mark.asyncio
async def test_complete_estimates_usage_for_str_handler() -> None:
    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "plain text"

    client = _make(handler)
    resp = await client.complete("judge", "root_cause", {"q": "x"})
    assert resp.usage.estimated is True
    assert resp.usage.input_tokens >= 1


@pytest.mark.asyncio
async def test_cassette_record_stores_real_usage(tmp_path) -> None:
    import json as _json

    from rsi.llm.cassette import CassetteStore

    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "ok", {"input_tokens": 7, "output_tokens": 9}

    cs = CassetteStore(root=tmp_path, mode="record")
    client = LLMClient(handler=handler, cassette=cs)
    await client.complete("judge", "root_cause", {"q": "y"})
    files = list(tmp_path.rglob("*.ndjson"))
    assert files, "cassette record should persist"
    lines = [
        _json.loads(ln)
        for ln in files[0].read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    entry = lines[-1]
    assert entry.get("input_tokens") == 7
    assert entry.get("output_tokens") == 9
