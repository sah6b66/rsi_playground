"""AnthropicCompatHandler（A2）：请求形状/响应拼接/错误路径."""

from unittest.mock import MagicMock

import pytest

from rsi.llm.providers import AnthropicCompatHandler


def _ok_response():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "content": [
            {"type": "text", "text": "部分一"},
            {"type": "text", "text": "部分二"},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return resp


def test_call_posts_messages_api(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return _ok_response()

    monkeypatch.setattr("rsi.llm.providers.httpx.post", fake_post)
    h = AnthropicCompatHandler(
        api_key="tk",
        base_url="https://open.bigmodel.cn/api/anthropic",
        model="glm-5.1",
    )
    out, usage = h("judge", "root_cause", {"prompt": "判卷"}, temperature=0.0)
    assert out == "部分一部分二"
    assert usage == {"input_tokens": 10, "output_tokens": 5}
    assert captured["url"] == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    body = captured["kw"]["json"]
    assert body["model"] == "glm-5.1" and body["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "判卷"}]
    assert captured["kw"]["headers"]["x-api-key"] == "tk"
    assert captured["kw"]["headers"]["Authorization"] == "Bearer tk"
    assert captured["kw"]["headers"]["anthropic-version"] == "2023-06-01"


def test_vars_without_prompt_falls_back_to_json(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured.update(kw)
        return _ok_response()

    monkeypatch.setattr("rsi.llm.providers.httpx.post", fake_post)
    AnthropicCompatHandler(api_key="tk")("judge", "root_cause", {"a": "b"})
    assert '"a"' in captured["json"]["messages"][0]["content"]


def test_http_error_raises(monkeypatch):
    resp = MagicMock(status_code=401, text="unauthorized")
    monkeypatch.setattr(
        "rsi.llm.providers.httpx.post", lambda url, **kw: resp
    )
    with pytest.raises(RuntimeError, match="401"):
        AnthropicCompatHandler(api_key="tk")(
            "judge", "root_cause", {"prompt": "x"}
        )


def test_empty_text_raises(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"content": []}
    monkeypatch.setattr(
        "rsi.llm.providers.httpx.post", lambda url, **kw: resp
    )
    with pytest.raises(RuntimeError, match="no text"):
        AnthropicCompatHandler(api_key="tk")(
            "judge", "root_cause", {"prompt": "x"}
        )


def test_handler_temperature_kwarg_accepted(monkeypatch):
    """LLMClient 探测到 **kwargs 时会传 temperature —— handler 必须收下."""
    monkeypatch.setattr(
        "rsi.llm.providers.httpx.post", lambda url, **kw: _ok_response()
    )
    h = AnthropicCompatHandler(api_key="tk")
    assert h("critic", "x", {"prompt": "p"}, temperature=0.3)


def test_handler_retries_on_5xx(monkeypatch):
    import httpx

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 3, "output_tokens": 4}}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        "rsi.llm.providers.time.sleep", lambda s: sleeps.append(s)
    )
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    out, usage = h("judge", "root_cause", {"prompt": "p"})
    assert out == "ok"
    assert usage == {"input_tokens": 3, "output_tokens": 4}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # 两次失败各退避一次
    assert all(0 < s <= 31 for s in sleeps)  # min(2^n,30)+抖动


def test_handler_raises_after_retry_exhaustion(monkeypatch):
    import httpx

    def fake_post(url, **kw):
        return httpx.Response(500, text="down")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("rsi.llm.providers.time.sleep", lambda s: None)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    from rsi.llm.providers import RetryableProviderError

    with pytest.raises(RetryableProviderError):
        h("judge", "root_cause", {"prompt": "p"})


def test_handler_no_retry_on_4xx(monkeypatch):
    import httpx

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    monkeypatch.setattr(httpx, "post", fake_post)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    from rsi.llm.providers import ProviderHTTPError

    with pytest.raises(ProviderHTTPError):
        h("judge", "root_cause", {"prompt": "p"})
    assert calls["n"] == 1  # 4xx 不重试


def test_handler_retries_on_429_then_succeeds(monkeypatch):
    """B2-T8：429 限流一次 → 退避重试 → 第二次 200 成功."""
    import httpx

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}],
                  "usage": {"input_tokens": 3, "output_tokens": 4}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("rsi.llm.providers.time.sleep", lambda s: None)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    out, usage = h("judge", "root_cause", {"prompt": "p"})
    assert out == "ok"
    assert usage == {"input_tokens": 3, "output_tokens": 4}
    assert calls["n"] == 2  # 429 一次重试后成功


def test_handler_retries_on_connect_timeout_then_succeeds(monkeypatch):
    """B2-T8：连接超时一次 → 退避重试 → 第二次 200 成功."""
    import httpx

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}],
                  "usage": {"input_tokens": 3, "output_tokens": 4}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("rsi.llm.providers.time.sleep", lambda s: None)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    out, usage = h("judge", "root_cause", {"prompt": "p"})
    assert out == "ok"
    assert calls["n"] == 2
