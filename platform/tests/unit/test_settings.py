"""Tests for rsi.settings (config doc §2)."""

import os
from pathlib import Path

import pytest


def _clean_env(monkeypatch):
    # Strip any ambient RSI_ env vars so defaults are deterministic.
    for key in list(os.environ):
        if key.startswith("RSI_"):
            monkeypatch.delenv(key, raising=False)


def test_default_settings(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    s = Settings()
    assert s.server.port == 8000
    assert s.server.host == "0.0.0.0"
    assert s.database.url.startswith("sqlite")
    assert s.git.repo.bare is True
    assert s.git.author.name == "rsi-platform"
    assert s.security.jwt_expire_hours == 12
    assert s.security.jwt_secret_env == "RSI_JWT_SECRET"
    assert s.scheduler.kind == "asyncio"
    assert s.seed.master == 20261102
    assert s.llm.cassette_mode == "replay"
    assert s.llm.max_concurrent_calls == 4
    assert s.judge.det_timeout_s == 120
    assert s.judge.pairwise_enabled is True
    assert s.optimizer.critic_max_repair_rounds == 5
    assert s.decision.alpha == 0.05
    assert s.decision.no_gain_rounds_default == 5
    assert s.orchestrator.max_parallel_batches == 2
    assert s.orchestrator.checkpoint_interval_s == 5
    assert s.carrier.task_timeout_s == 1800
    assert s.sandbox.build_retry == 2
    assert s.sandbox.ready_timeout_s == 600
    assert s.tracing.collector.grpc == 4317
    assert s.instrumentor.policy.prefer[0] == "hooks"


def test_env_override(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_SERVER__PORT", "9000")
    s = Settings()
    assert s.server.port == 9000


def test_rsi_home_path_resolution(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    s = Settings()
    assert s.home == tmp_path
    # Relative storage paths resolve against RSI_HOME.
    assert s.storage.traces_dir == tmp_path / "traces"
    assert s.storage.stop_file == tmp_path / "STOP"
    # git section paths default under RSI_HOME too.
    assert s.git.repo.path == tmp_path / "git" / "platform-repo.git"
    assert s.git.worktrees == tmp_path / "git" / "wt"


def test_default_rsi_home(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings, DEFAULT_HOME

    s = Settings()
    assert s.home == Path(DEFAULT_HOME).resolve()


def test_absolute_path_not_rebased(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.setenv("RSI_STORAGE__TRACES_DIR", str(tmp_path / "elsewhere"))
    s = Settings()
    assert s.storage.traces_dir == tmp_path / "elsewhere"


def test_nested_env_override(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_JUDGE__LLM_RETRY", "5")
    monkeypatch.setenv("RSI_DECISION__ALPHA", "0.01")
    s = Settings()
    assert s.judge.llm_retry == 5
    assert s.decision.alpha == 0.01
