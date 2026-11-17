"""B1-6：backup_provider 故障转移（DD-02 §5.1 ProviderDown → switch）."""
from __future__ import annotations

import pytest

from rsi.llm.providers import ProviderChain, RetryableProviderError


class _Handler:
    def __init__(self, result, raise_retryable=False):
        self._result = result
        self._raise = raise_retryable
        self.calls = 0

    def __call__(self, component, prompt_name, vars, *, temperature=0.0):
        self.calls += 1
        if self._raise:
            raise RetryableProviderError("primary down")
        return self._result


def test_chain_falls_back_to_backup():
    primary = _Handler(None, raise_retryable=True)
    backup = _Handler("from-backup")
    chain = ProviderChain(primary, backup)
    assert chain("judge", "p", {"prompt": "x"}) == "from-backup"
    assert primary.calls == 1 and backup.calls == 1


def test_chain_primary_success_never_touches_backup():
    primary = _Handler("from-primary")
    backup = _Handler("unused")
    chain = ProviderChain(primary, backup)
    assert chain("judge", "p", {"prompt": "x"}) == "from-primary"
    assert backup.calls == 0


def test_chain_raises_when_both_down():
    chain = ProviderChain(
        _Handler(None, raise_retryable=True),
        _Handler(None, raise_retryable=True),
    )
    with pytest.raises(RetryableProviderError):
        chain("judge", "p", {"prompt": "x"})


def test_chain_passes_through_non_retryable():
    from rsi.llm.providers import ProviderHTTPError

    def bad(component, prompt_name, vars, *, temperature=0.0):
        raise ProviderHTTPError("401")

    chain = ProviderChain(bad, _Handler("unused"))
    with pytest.raises(ProviderHTTPError):
        chain("judge", "p", {"prompt": "x"})


class TestBootstrapPerProviderModel:
    """B2-T8：主/备 handler 各取自己 provider.model，空值回退 judge 默认.

    旧行为：备 model 恒取 judge default → 跨族备胎（不同厂商 model 名）
    404。修复后 provider 配置自己的 model 键。
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))

    def _settings(self):
        from rsi.settings import Settings

        s = Settings()
        s.llm.cassette_mode = "off"
        return s

    def _build(self, s):
        from rsi.bootstrap import build_services

        return build_services(s)

    def test_primary_and_backup_use_own_provider_model(self):
        from rsi.llm.providers import ProviderChain
        from rsi.settings import LlmProviderSettings

        s = self._settings()
        s.llm.providers["anthropic"] = LlmProviderSettings(
            api_key="k1", model="glm-5.2", backup_provider="openai_compat"
        )
        s.llm.providers["openai_compat"] = LlmProviderSettings(
            api_key="k2", base_url="http://backup.example", model="qwen-max"
        )
        handler = self._build(s)["llm_client"]._handler
        assert isinstance(handler, ProviderChain)
        assert handler._primary.model == "glm-5.2"
        assert handler._backup is not None
        assert handler._backup.model == "qwen-max"  # 不再抄 judge 默认

    def test_empty_provider_model_falls_back_to_judge_default(self):
        from rsi.settings import LlmModelSettings, LlmProviderSettings

        s = self._settings()
        s.llm.defaults["judge"] = LlmModelSettings(model="glm-4.7")
        s.llm.providers["anthropic"] = LlmProviderSettings(
            api_key="k1", backup_provider="openai_compat"
        )
        s.llm.providers["openai_compat"] = LlmProviderSettings(api_key="k2")
        handler = self._build(s)["llm_client"]._handler
        assert handler._primary.model == "glm-4.7"
        assert handler._backup.model == "glm-4.7"
