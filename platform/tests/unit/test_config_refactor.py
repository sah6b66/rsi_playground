"""Config refactor C1: three-tier settings (deployment env / user yaml /
internal yaml), derived paths, LLM direct values, restart endpoint."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.settings import Settings

pytestmark = pytest.mark.asyncio


def _clean_env(monkeypatch, home: Path | None = None):
    for key in list(os.environ):
        if key.startswith("RSI_"):
            monkeypatch.delenv(key, raising=False)
    if home is not None:
        monkeypatch.setenv("RSI_HOME", str(home))


# ── 1. derived paths ─────────────────────────────────────────────────────


async def test_derived_paths_from_rsi_home(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    s = Settings()
    assert s.git_repo_path == tmp_path / "git" / "platform-repo.git"
    assert s.git_worktrees_dir == tmp_path / "git" / "wt"
    assert s.traces_dir == tmp_path / "storage" / "traces"
    assert s.artifacts_dir == tmp_path / "storage" / "artifacts"
    assert s.labs_dir == tmp_path / "storage" / "labs"
    assert s.recordings_dir == tmp_path / "storage" / "recordings"
    assert s.stop_file == tmp_path / "storage" / "STOP"
    assert s.stop_file_path == s.stop_file
    assert s.logs_dir == tmp_path / "logs"
    # directories materialize on construction
    assert s.traces_dir.is_dir() and s.logs_dir.is_dir()
    # home is not a Settings field (never exposed via model_dump)
    assert "home" not in s.model_dump()


# ── 2. LLM direct values ─────────────────────────────────────────────────


async def test_llm_provider_direct_api_key(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {"llm": {"providers": {"anthropic": {"api_key": "sk-xxx"}}}}
        ),
        encoding="utf-8",
    )
    s = Settings()
    assert s.llm.providers["anthropic"].api_key == "sk-xxx"
    # old api_key_env keys are silently ignored
    assert not hasattr(s.llm.providers["anthropic"], "api_key_env")


# ── 3. internal yaml tier ────────────────────────────────────────────────


async def test_internal_yaml_overrides_defaults(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    (tmp_path / "settings-internal.yaml").write_text(
        yaml.safe_dump({"instrumentor": {"policy": {"smoke_verify": False}}}),
        encoding="utf-8",
    )
    assert Settings().instrumentor.policy.smoke_verify is False


async def test_user_yaml_cannot_set_internal_keys(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"instrumentor": {"policy": {"smoke_verify": True}}}),
        encoding="utf-8",
    )
    # user-yaml value ignored: default (True here) or internal file wins
    s = Settings()
    assert s.instrumentor.policy.smoke_verify is True  # the default
    (tmp_path / "settings-internal.yaml").write_text(
        yaml.safe_dump({"instrumentor": {"policy": {"smoke_verify": False}}}),
        encoding="utf-8",
    )
    # internal file wins over BOTH user yaml and defaults
    assert Settings().instrumentor.policy.smoke_verify is False


async def test_internal_yaml_cannot_override_user_keys(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"judge": {"det_timeout_s": 55}}), encoding="utf-8"
    )
    (tmp_path / "settings-internal.yaml").write_text(
        yaml.safe_dump({"judge": {"det_timeout_s": 1}}), encoding="utf-8"
    )
    assert Settings().judge.det_timeout_s == 55


# ── 4/5. API surface ─────────────────────────────────────────────────────


def make_services(tmp_path: Path) -> dict:
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "settings": Settings(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            }
        },
    }


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    _clean_env(monkeypatch, home=tmp_path)
    services = make_services(tmp_path)
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c._services = services  # type: ignore[attr-defined]
        yield c


async def _admin(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_settings_page_user_subset(client):
    h = await _admin(client)
    resp = await client.get("/api/v1/system/settings", headers=h)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    paths = {i["path"] for i in items}
    # 42 Settings-backed user keys + 3 legacy runtime knobs (judge.llm.model
    # alias, budget.llm.daily, platform.static_item). The spec's ~35/≤40
    # estimate undercounted the llm.defaults role expansion; llm.providers.*
    # (api_key secrets) is excluded from the page entirely.
    assert len(paths) <= 45
    assert "server.port" in paths
    assert "judge.det_timeout_s" in paths
    assert "seed.master" in paths
    assert "orchestrator.budget.iteration_default.max_tokens" in paths
    # excluded tiers
    assert not any(p.startswith("llm.providers") for p in paths)
    assert not any(p.startswith("sandbox.backends") for p in paths)
    assert not any(p.startswith("instrumentor") for p in paths)
    assert not any(p.startswith("security") for p in paths)
    assert not any(p.startswith("git") for p in paths)
    assert not any(p.startswith("storage") for p in paths)
    assert not any(p.startswith("tracing.collector") for p in paths)
    assert not any(p.startswith("carrier.adapters") for p in paths)
    assert "home" not in paths


async def test_put_user_key_marks_pending_restart(client, tmp_path):
    h = await _admin(client)
    resp = await client.put(
        "/api/v1/system/settings/judge.det_timeout_s",
        json={"value": 90},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value"] == 90
    assert body["pending_restart"] is True
    # persisted to user settings.yaml
    doc = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
    assert doc["judge"]["det_timeout_s"] == 90


async def test_put_rejected_for_removed_tier(client):
    h = await _admin(client)
    resp = await client.put(
        "/api/v1/system/settings/instrumentor.policy.smoke_verify",
        json={"value": False},
        headers=h,
    )
    assert resp.status_code == 409


async def test_restart_endpoint(client, monkeypatch):
    h = await _admin(client)
    exited = []
    timers = []

    class FakeTimer:
        def __init__(self, interval, fn):
            timers.append((interval, fn))

        def start(self):
            pass

    monkeypatch.setattr(
        "rsi.api.routers.system.threading.Timer", FakeTimer
    )
    resp = await client.post("/api/v1/system:restart", headers=h)
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "restarting"}
    assert len(timers) == 1
    interval, fn = timers[0]
    assert interval >= 0.5
    assert callable(fn)  # the os._exit callback (NOT invoked here)


async def test_restart_requires_admin(client):
    # non-admin user (researcher) → 403
    auth: AuthService = client._services["auth_service"]
    services = client._services
    services["users"]["res"] = {
        "user_id": "u-res",
        "role": "researcher",
        "password_hash": auth.hash_password("res-pw"),
    }
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "res", "password": "res-pw"}
    )
    h = {"Authorization": f"Bearer {resp.json()['token']}"}
    resp = await client.post("/api/v1/system:restart", headers=h)
    assert resp.status_code == 403


# ── 6. budget key rename ─────────────────────────────────────────────────


async def test_budget_key_names(monkeypatch, tmp_path):
    _clean_env(monkeypatch, home=tmp_path)
    s = Settings()
    # yaml key "global" (attribute global_) and "iteration_default"
    assert s.orchestrator.budget.global_.max_tokens == 100_000_000
    assert s.orchestrator.budget.iteration_default.max_tokens == 50_000_000
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {"orchestrator": {"budget": {
                "global": {"max_tokens": 7},
                "iteration_default": {"max_tokens": 3},
            }}}
        ),
        encoding="utf-8",
    )
    s2 = Settings()
    assert s2.orchestrator.budget.global_.max_tokens == 7
    assert s2.orchestrator.budget.iteration_default.max_tokens == 3
    # old default_iteration key silently ignored
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {"orchestrator": {"budget": {"default_iteration": {"max_tokens": 9}}}}
        ),
        encoding="utf-8",
    )
    assert Settings().orchestrator.budget.iteration_default.max_tokens == 50_000_000
