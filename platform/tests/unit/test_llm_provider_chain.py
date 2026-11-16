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
