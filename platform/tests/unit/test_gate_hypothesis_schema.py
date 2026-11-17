"""B2-T5：人工门②接受 FR6.3 全 schema 草稿（B2-T4 起草产物的落点）.

POST /iterations/{id}/hypothesis body 可携带 ``draft``（FR6.3 全字段）——
经 HypothesisDraftOut 校验后写入 hypotheses 记录 ``hypothesis_json``
（内存 store + persist_record 持久化）；纯字符串老路径完全不变；
无效 draft → 422 列字段且迭代状态不被推进；engine 活跃路径
gate_hypothesis 载荷同步合并 draft 字段。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio

_DRAFT_FULL = {
    "problem_trace_ids": ["trace-1"],
    "failure_type": "missed_critical_command",
    "defect_class": "A",
    "proposal_type": "modify_prompt",
    "diff_content": "--- a/system-prompt.md\n+++ b/system-prompt.md\n"
    "+步骤3 必须执行 display interface brief",
    "expected_improve": "task_complete +0.1",
    "risk": "低",
    "verification_task_ids": ["task-9"],
    "predicted_affected_tasks": ["task-3"],
}


def make_services() -> dict:
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
        },
    }


def _seed_iteration(stores: dict, it_id: str) -> None:
    """预置一条 paused 在 awaiting_hypothesis 的迭代（人工门②等待位）."""
    stores["iterations"][it_id] = {
        "id": it_id,
        "sut_id": "sut-1",
        "profile_id": "prof-1",
        "evalset_id": None,
        "profile": None,
        "label": None,
        "prep_mode": "cli",
        "api_evalset_name": None,
        "mode": "p1",
        "state": "paused",
        "current_step": "awaiting_hypothesis",
        "round": 1,
        "experiments": [],
        "budget_used": {"llm_calls": 0, "tokens": 0},
        "archived": False,
        "paused_from": "attributing",
        "gates": [],
        "created_at": "2026-11-16T00:00:00+00:00",
    }


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    app = create_app(make_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c._app = app
        _seed_iteration(c._app.state.stores, "iter-1")
        _seed_iteration(c._app.state.stores, "iter-2")
        yield c


async def _hdr(client) -> dict:
    r = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_gate_hypothesis_accepts_full_draft(client):
    h = await _hdr(client)
    resp = await client.post(
        "/api/v1/iterations/iter-1/hypothesis",
        headers=h,
        json={"hypothesis": "补步骤3指引", "draft": _DRAFT_FULL},
    )
    assert resp.status_code == 201, resp.text
    stores = client._app.state.stores
    hyp = stores["hypotheses"][resp.json()["hypothesis_id"]]
    assert hyp["statement"] == "补步骤3指引"
    assert hyp["hypothesis_json"]["proposal_type"] == "modify_prompt"
    assert hyp["hypothesis_json"]["predicted_affected_tasks"] == ["task-3"]
    assert hyp["hypothesis_json"]["defect_class"] == "A"
    # 迭代状态推进到下一门等待位（review）
    it = stores["iterations"]["iter-1"]
    assert it["state"] == "attributing"
    assert it["current_step"] == "awaiting_review"


async def test_gate_hypothesis_legacy_string_still_works(client):
    resp = await client.post(
        "/api/v1/iterations/iter-2/hypothesis",
        headers=await _hdr(client),
        json={"hypothesis": "仅文字"},
    )
    assert resp.status_code == 201, resp.text
    stores = client._app.state.stores
    hyp = stores["hypotheses"][resp.json()["hypothesis_id"]]
    assert hyp["statement"] == "仅文字"
    # 老路径不携带 FR6.3 字段
    assert "hypothesis_json" not in hyp


async def test_gate_hypothesis_invalid_draft_422(client):
    """draft 不过 schema → 422 列字段，且迭代状态不被推进."""
    h = await _hdr(client)
    bad = {**_DRAFT_FULL, "defect_class": "C"}
    resp = await client.post(
        "/api/v1/iterations/iter-1/hypothesis",
        headers=h,
        json={"hypothesis": "补步骤3指引", "draft": bad},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "VALIDATION"
    assert "defect_class" in body["message"]
    # 422 前置校验：迭代保持原 paused 等待位，未产生 hypotheses 记录
    it = client._app.state.stores["iterations"]["iter-1"]
    assert it["state"] == "paused"
    assert it["current_step"] == "awaiting_hypothesis"
    assert client._app.state.stores["hypotheses"] == {}


class _StubEngine:
    """gate_hypothesis 记录载荷并回铸 id（模拟活跃引擎 D-3 桥接）."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def gate_hypothesis(self, hyp: dict) -> str:
        self.payloads.append(dict(hyp))
        return "hyp-engine-1"


async def test_gate_hypothesis_engine_path_merges_draft(client):
    from rsi.orchestrator.iteration import RUNNING_ENGINES

    engine = _StubEngine()
    RUNNING_ENGINES["iter-2"] = engine
    try:
        resp = await client.post(
            "/api/v1/iterations/iter-2/hypothesis",
            headers=await _hdr(client),
            json={"hypothesis": "引擎侧", "draft": _DRAFT_FULL},
        )
    finally:
        RUNNING_ENGINES.pop("iter-2", None)
    assert resp.status_code == 201, resp.text
    assert resp.json()["hypothesis_id"] == "hyp-engine-1"
    # gate_hypothesis 载荷合并 FR6.3 全字段（summary 保留）
    sent = engine.payloads[-1]
    assert sent["summary"] == "引擎侧"
    assert sent["proposal_type"] == "modify_prompt"
    assert sent["predicted_affected_tasks"] == ["task-3"]
    # store 记录同样带 hypothesis_json
    hyp = client._app.state.stores["hypotheses"]["hyp-engine-1"]
    assert hyp["hypothesis_json"]["proposal_type"] == "modify_prompt"
