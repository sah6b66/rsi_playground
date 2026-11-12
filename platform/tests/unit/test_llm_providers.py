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
    out = h("judge", "root_cause", {"prompt": "判卷"}, temperature=0.0)
    assert out == "部分一部分二"
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
