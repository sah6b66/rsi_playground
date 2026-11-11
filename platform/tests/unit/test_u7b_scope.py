"""U7b: per-agent optimization scope (explicit RSI-optimizable paths).

API: POST /suts + PUT /suts/{id}/optimization-scope record/validate the
scope; executor layer (DeterministicCritic / ChangeApplier / engine wiring)
enforces it at RSI-optimization time only — manual edits stay bounded by
the global whitelist.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.core.errors import TargetViolationError
from rsi.optimizer.applier import ChangeApplier
from rsi.optimizer.critic import DeterministicCritic
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.iteration import IterationEngine
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio

# -- API tests ----------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    auth = AuthService(secret="test-secret")
    app = create_app(
        {
            "auth_service": auth,
            "audit_log": AuditLog(),
            "users": {
                "admin": {
                    "user_id": "u-admin",
                    "role": "admin",
                    "password_hash": auth.hash_password("admin-pw"),
                }
            },
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
        )
        c.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        yield c


async def test_create_sut_with_scope_recorded(client: AsyncClient):
    resp = await client.post(
        "/api/v1/suts",
        json={
            "name": "scoped-agent",
            "carrier": "opencode",
            "image_ref": "img:v1",
            "optimization_scope": ["skills/bgp-diag/", "mcp.json"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["optimization_scope"] == ["skills/bgp-diag/", "mcp.json"]
    got = await client.get(f"/api/v1/suts/{body['id']}")
    assert got.json()["optimization_scope"] == ["skills/bgp-diag/", "mcp.json"]


async def test_create_sut_scope_invalid_items_422(client: AsyncClient):
    resp = await client.post(
        "/api/v1/suts",
        json={
            "name": "bad-scope",
            "carrier": "opencode",
            "image_ref": "img:v1",
            "optimization_scope": ["skills/ok/", "judge/x", "nope"],
        },
    )
    assert resp.status_code == 422
    msg = resp.json()["message"]
    assert "judge/x" in msg and "nope" in msg and "skills/ok/" not in msg


async def test_put_optimization_scope(client: AsyncClient):
    created = (
        await client.post(
            "/api/v1/suts",
            json={"name": "scope-edit", "carrier": "opencode", "image_ref": "i:v1"},
        )
    ).json()
    assert created["optimization_scope"] is None  # 未定义 = 自动模式

    resp = await client.put(
        f"/api/v1/suts/{created['id']}/optimization-scope",
        json={"optimization_scope": ["scripts/"]},
    )
    assert resp.status_code == 200
    assert resp.json()["optimization_scope"] == ["scripts/"]

    # 非法项 → 422；空列表 → 恢复自动模式（None）
    bad = await client.put(
        f"/api/v1/suts/{created['id']}/optimization-scope",
        json={"optimization_scope": ["judge/x"]},
    )
    assert bad.status_code == 422
    reset = await client.put(
        f"/api/v1/suts/{created['id']}/optimization-scope",
        json={"optimization_scope": []},
    )
    assert reset.status_code == 200
    assert reset.json()["optimization_scope"] is None


# -- executor-layer tests ------------------------------------------------------


def test_critic_scope_narrows():
    critic = DeterministicCritic()
    enabled = {"skills", "scripts"}
    inside = [{"path": "suts/a/skills/a/x.md"}]
    outside = [{"path": "suts/a/scripts/y.py"}]

    # scope=skills/a/ → skills/a/x.md 过；scripts/y.py 越界失败
    assert critic.check(inside, enabled, allowed_paths=["skills/a/"]) == []
    failures = critic.check(outside, enabled, allowed_paths=["skills/a/"])
    assert any("out_of_scope" in f and "超出该 Agent 的优化范围" in f for f in failures)
    # None（自动模式）→ 不限制
    assert critic.check(outside, enabled, allowed_paths=None) == []


def test_critic_scope_dir_prefix_not_segment_prefix():
    critic = DeterministicCritic()
    # skills/a/ 不得放行 skills/ab/y.md（目录前缀含斜杠）
    failures = critic.check(
        [{"path": "suts/a/skills/ab/y.md"}],
        {"skills"},
        allowed_paths=["skills/a/"],
    )
    assert failures and "out_of_scope" in failures[0]


def test_applier_scope_enforced():
    applier = ChangeApplier()
    hyp = {
        "hypothesis_id": "hyp-1",
        "diff_content": "--- a/x\n+++ b/scripts/y.py\n@@\n-x\n+y\n",
    }
    with pytest.raises(TargetViolationError) as exc:
        applier.apply(
            "sut-a", hyp, {"scripts"}, allowed_paths=["skills/a/"]
        )
    assert "超出该 Agent 的优化范围" in str(exc.value)
    assert exc.value.context.get("reason") == "out_of_scope"
    # scope 内 → 正常返回候选版本 id
    vid = applier.apply("sut-a", hyp, {"scripts"}, allowed_paths=["scripts/"])
    assert vid


async def test_engine_passes_scope_to_critic():
    """引擎集成：sut_scope_fn 的 scope 到达 Critic（越界 → critic 拒绝短路）。"""
    from types import SimpleNamespace

    class StubAttribution:
        def analyze(self, failed_runs, sut_config, opt_history):
            return SimpleNamespace(clusters=[])

    class StubSelector:
        def decide(self, ctx):
            from rsi.core.models import Decision

            return Decision(action="REVERT", rule_hit=0, reason="stub", evidence={})

    class StubBudget:
        async def record(self, *a, **kw):
            pass

    class StubRunner:
        async def run_batch(self, *a, **kw):
            raise AssertionError("critic rejection must short-circuit before batches")

    engine = IterationEngine(
        batch_runner=StubRunner(),
        attribution_analyzer=StubAttribution(),
        selector=StubSelector(),
        event_bus=EventBus(),
        budget=StubBudget(),
        critic=DeterministicCritic(),
        sut_scope_fn=lambda sut_id: ["skills/a/"],
    )
    from rsi.core.models import OptimizationProfile, TaskRecord

    profile = OptimizationProfile(
        name="p",
        evalsets={"smoke": "", "iterate": "", "holdout": "es"},
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        enabled_targets={"skills", "scripts"},
    )
    hyp = {
        "summary": "touch outside scope",
        "changeset_files": [
            {"path": "suts/sut-1/skills/a/x.md"},
            {"path": "suts/sut-1/scripts/y.py"},
        ],
    }
    task = TaskRecord.model_validate(
        {
            "task_id": "fault/t-01",
            "title": "t",
            "task": {"prompt": "p"},
            "expected": {"root_cause": {"expected": "x"}},
        }
    )
    out = await engine.iterate_once(
        "sut-1", profile, None, [task], "cfg-v1", "img", hypothesis=hyp
    )
    assert out["short_circuit"] == "critic_repair_exhausted"
    assert "out_of_scope" in out["decision"].reason
