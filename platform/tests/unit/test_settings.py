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
    assert s.database.url == ""
    assert s.database_url.startswith("sqlite:///")  # <RSI_HOME>/platform.db
    assert s.git.repo.bare is True
    assert s.git.author.name == "rsi-platform"
    # C1: security section removed (deployment env tier); scheduler stays
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


def test_default_sandbox_backends(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    b = Settings().sandbox.backends
    assert set(b) == {"fake", "ensp-pro-1", "pnetlab"}
    assert b["fake"].kind == "fake" and b["fake"].url == ""
    assert b["ensp-pro-1"].kind == "ensp-pro"
    assert b["ensp-pro-1"].url == "https://192.168.56.101:8443"
    assert b["ensp-pro-1"].verify_tls is False
    assert b["ensp-pro-1"].subnets == ["10.101.0.0/16"]
    assert b["pnetlab"].kind == "pnetlab"
    assert b["pnetlab"].url == "http://192.168.56.102"


def test_sandbox_backend_legacy_keys_ignored(monkeypatch, tmp_path):
    """Old SSH-shaped YAML loads silently (extra=ignore)."""
    _clean_env(monkeypatch)
    import yaml
    from rsi.settings import Settings

    cfg = yaml.safe_load("""
sandbox:
  backends:
    ensp-pro-1:
      host: 192.168.1.50
      ssh_port: 22
      ssh_user: administrator
      agent_cmd: pg-enspctl
      kind: ensp-pro
      url: https://192.0.2.1:8443
""")
    s = Settings(**cfg)
    b = s.sandbox.backends["ensp-pro-1"]
    assert b.kind == "ensp-pro"
    assert b.url == "https://192.0.2.1:8443"
    assert not hasattr(b, "host") and not hasattr(b, "ssh_user")



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
    # C1: paths are derived from RSI_HOME (no longer Settings fields).
    assert s.traces_dir == tmp_path / "storage" / "traces"
    assert s.stop_file == tmp_path / "storage" / "STOP"
    assert s.git_repo_path == tmp_path / "git" / "platform-repo.git"
    assert s.git_worktrees_dir == tmp_path / "git" / "wt"


def test_default_rsi_home(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings, DEFAULT_HOME

    s = Settings()
    assert s.home == Path(DEFAULT_HOME).resolve()


def test_absolute_path_not_rebased(monkeypatch, tmp_path):
    """C1: storage paths are derived from RSI_HOME; the old
    RSI_STORAGE__TRACES_DIR env override no longer has a backing field and
    is silently ignored."""
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.setenv("RSI_STORAGE__TRACES_DIR", str(tmp_path / "elsewhere"))
    s = Settings()
    assert not hasattr(s, "storage")
    assert s.traces_dir == tmp_path / "storage" / "traces"


def test_nested_env_override(monkeypatch):
    _clean_env(monkeypatch)
    from rsi.settings import Settings

    monkeypatch.setenv("RSI_JUDGE__LLM_RETRY", "5")
    monkeypatch.setenv("RSI_DECISION__ALPHA", "0.01")
    s = Settings()
    assert s.judge.llm_retry == 5
    assert s.decision.alpha == 0.01
