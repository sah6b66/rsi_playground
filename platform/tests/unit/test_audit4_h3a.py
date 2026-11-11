"""audit4 H3-A: D-10/N-2 real-evalset batch resolver + N-3 noise calibration."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import SQLModel, select

from rsi.core.errors import NotFoundError, ValidationError
from rsi.orchestrator.resolver import build_batch_resolver, build_noise_run_batch_fn
from rsi.settings import DatabaseSettings, Settings

_ENV_YAML = """
name: env-a4
backend: fake
devices:
  - {name: R1, type: vrp, config: "configs/R1.cfg"}
  - {name: R2, type: vrp, config: "configs/R2.cfg"}
topology:
  links: []
ssh_access:
  R1: {ip: 10.0.0.1, port: 2001, username: admin, password: pw}
  R2: {ip: 10.0.0.2, port: 2002, username: admin, password: pw}
"""

_TASKS = [
    {"id": "fault/bgp-01", "prompt": "diagnose why BGP peer 10.0.0.2 is down"},
    {"id": "fault/ospf-02", "prompt": "check OSPF neighbors on R2"},
    {"id": "inspection/link-03", "prompt": "report interface error counters"},
]


def _evalset() -> dict:
    return {
        "id": "es-a4",
        "name": "a4",
        "purpose": "iterate",
        "tasks": [dict(t) for t in _TASKS],
        "env_yaml": _ENV_YAML,
        "configs": {"R1.cfg": "hostname R1"},
        "frozen": False,
        "version": 1,
        "created_at": "2026-11-05T00:00:00Z",
        "versions": [],
    }


def _stores() -> dict:
    return {
        "evalsets": {"es-a4": _evalset()},
        "suts": {"sut-a4": {"id": "sut-a4", "image_ref": "ipops-agent:v1.2"}},
    }


def _session_factory(engine):
    from rsi.db import get_session

    return functools.partial(get_session, engine)


# ── D-10/N-2: resolver unit ──────────────────────────────────────────────


def test_resolver_loads_real_env_tasks_image() -> None:
    stores = _stores()
    resolve = build_batch_resolver(evalsets_store=stores["evalsets"], suts_store=stores["suts"])
    env, tasks, cfg, img = resolve({"evalset_id": "es-a4", "sut_id": "sut-a4", "params": {}})
    assert [d.name for d in env.devices] == ["R1", "R2"]
    assert env.ssh_access["R2"].port == 2002
    assert [t.task_id for t in tasks] == [t["id"] for t in _TASKS]
    assert tasks[0].task.prompt.startswith("diagnose why BGP")
    assert img == "ipops-agent:v1.2"
    assert cfg == ""  # no config version -> documented empty-id degradation


def test_resolver_missing_evalset_raises_not_found() -> None:
    resolve = build_batch_resolver(evalsets_store={})
    with pytest.raises(NotFoundError):
        resolve({"evalset_id": "es-nope", "sut_id": "sut-a4"})


def test_resolver_no_payload_degrades_to_canned_default() -> None:
    stores = {
        "evalsets": {"es-bare": {"id": "es-bare", "tasks": []}},
        "suts": {},
    }
    resolve = build_batch_resolver(evalsets_store=stores["evalsets"], suts_store=stores["suts"])
    batch = {"evalset_id": "es-bare", "sut_id": "sut-a4", "params": {}}
    env, tasks, cfg, img = resolve(batch)
    assert env.backend == "fake"
    assert [t.task_id for t in tasks] == ["fault/adhoc-00"]
    assert img == "sut-a4"  # sut-id fallback (no suts record)
    # honest degradation marker is written back onto the batch params
    assert batch["params"]["env_source"] == "default_offline"


def test_resolver_bad_task_row_lists_line_number() -> None:
    stores = {
        "evalsets": {"es-bad": {"id": "es-bad", "tasks": [
            {"id": "fault/ok-01", "prompt": "fine"},
            {"id": "NOT A VALID ID", "prompt": "bad"},
        ], "env_yaml": _ENV_YAML}},
        "suts": {},
    }
    resolve = build_batch_resolver(evalsets_store=stores["evalsets"])
    with pytest.raises(ValidationError, match="line 2"):
        resolve({"evalset_id": "es-bad", "sut_id": "sut-a4"})


# ── N-3: run_batch_fn bridge + calibrator persistence ────────────────────


class _Stats:
    def __init__(self, task_scores):
        self.task_scores = task_scores
        self.n = len(task_scores)
        self.metrics = {}


class _StubRunner:
    def __init__(self) -> None:
        self.batch_ids: list[str] = []

    async def run_batch(self, batch_id, env, tasks, cfg, img, **kw):
        self.batch_ids.append(batch_id)
        assert env is not None and cfg == ""
        return _Stats([
            {"task_id": t.task_id, "score": 0.5, "task_complete": 1.0}
            for t in tasks
        ])


@pytest.mark.asyncio
async def test_noise_bridge_runs_calib_batches_per_pass(tmp_path: Path) -> None:
    from rsi.db import create_engine
    from rsi.decision.noise import NoiseBandCalibrator
    from rsi.db.tables import NoiseBand

    engine = create_engine(f"sqlite:///{tmp_path/'nb.db'}")
    SQLModel.metadata.create_all(engine)
    session_factory = _session_factory(engine)
    stores = _stores()
    resolve = build_batch_resolver(evalsets_store=stores["evalsets"], suts_store=stores["suts"])
    runner = _StubRunner()
    calls = {"n": 0}

    async def varying_fn(task_ids):
        calls["n"] += 1
        # alternate scores per pass so sigma > 0
        return {tid: {"score": 0.4 + 0.1 * (calls["n"] % 2)} for tid in task_ids}

    calibrator = NoiseBandCalibrator(session_factory=session_factory)
    info = await calibrator.calibrate(
        "sut-a4", evalset_id="es-a4", k=3, run_batch_fn=varying_fn,
        task_index={t["id"]: t for t in _TASKS},
    )
    assert info.k == 3
    assert info.metrics["score"]["sigma"] > 0
    with session_factory() as session:
        rows = session.exec(select(NoiseBand)).all()
    assert rows, "noise_bands rows were not persisted"
    assert all(r.evalset_id == "es-a4" for r in rows)
    assert any(r.metric == "score" and r.sigma > 0 for r in rows)

    # bridge itself: batch ids carry the noise-calib prefix (bypass trigger)
    fn = build_noise_run_batch_fn(resolve, runner, "es-a4", "sut-a4")
    out = await fn(["fault/bgp-01", "inspection/link-03"])
    assert set(out) == {"fault/bgp-01", "inspection/link-03"}
    assert all(b.startswith("noise-calib-es-a4-") for b in runner.batch_ids)


# ── D-10: production assembly POST /batches runs real tasks ──────────────


@pytest.mark.asyncio
async def test_bootstrap_post_batches_runs_real_evalset_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    from rsi.api.app import create_app
    from rsi.bootstrap import build_services

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path/'t.db'}"),
    )
    services = build_services(settings)
    SQLModel.metadata.create_all(services["engine"])
    assert services["batch_runner"].batch_resolver is not None
    assert services["stores"]["evalsets"] is not None  # app-shared stores

    # pre-register sut + imported evalset (three-piece payload) in the
    # app-owned stores the resolver reads
    services["stores"]["suts"]["sut-a4"] = {"id": "sut-a4", "image_ref": "ipops:v1"}
    services["stores"]["evalsets"]["es-a4"] = _evalset()

    # record the task ids the judge actually receives
    seen: list[str] = []
    judge = services["judge_service"]
    orig = judge.judge_run

    async def recording(task, *a, **kw):
        if task is not None:
            seen.append(task.task.task_id)
        return await orig(task, *a, **kw)

    judge.judge_run = recording  # type: ignore[method-assign]

    auth = services["auth_service"]
    token, _ = auth.login("u-admin", "admin", auth.hash_password("pw"), "pw")
    app = create_app(services)
    runner = services["batch_runner"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/batches",
            json={"evalset_id": "es-a4", "sut_id": "sut-a4", "purpose": "iterate"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["id"]
        if runner._tasks:
            await asyncio.wait_for(
                asyncio.gather(*runner._tasks, return_exceptions=True), timeout=60
            )
        got = await client.get(
            f"/api/v1/batches/{batch_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = got.json()
        assert body["status"] == "finalized", body
        assert body["progress"]["done"] == len(_TASKS)
    # the judged tasks are exactly the evalset's jsonl task ids
    assert sorted(seen) == sorted(t["id"] for t in _TASKS)


# ── N-3: POST /dashboard/noise-bands:calibrate endpoint ──────────────────


def _calib_services(tmp_path: Path, stub_scores=None) -> dict:
    from rsi.api.deps import default_stores
    from rsi.db import create_engine
    from rsi.decision.noise import NoiseBandCalibrator
    from rsi.security.auth import AuthService

    engine = create_engine(f"sqlite:///{tmp_path/'cal.db'}")
    SQLModel.metadata.create_all(engine)
    session_factory = _session_factory(engine)
    stores = default_stores()
    stores["evalsets"]["es-a4"] = _evalset()
    run_i = {"n": 0}

    def factory(evalset_id, sut_id):
        async def run_batch_fn(task_ids):
            run_i["n"] += 1
            if stub_scores is not None:
                vals = {tid: {"score": stub_scores(run_i["n"])} for tid in task_ids}
            else:
                vals = {tid: {"score": 0.5} for tid in task_ids}
            return vals

        return run_batch_fn

    return {
        "auth_service": AuthService(secret="t"),
        "audit_log": None,
        "users": {},
        "stores": stores,
        "session_factory": session_factory,
        "noise_band": NoiseBandCalibrator(session_factory=session_factory),
        "noise_run_batch_fn": factory,
    }


@pytest.mark.asyncio
async def test_calibrate_endpoint_persists_and_reads(tmp_path: Path) -> None:
    from rsi.api.app import create_app
    from rsi.security.auth import AuthService

    services = _calib_services(tmp_path, stub_scores=lambda i: 0.4 + 0.1 * (i % 2))
    auth: AuthService = services["auth_service"]
    token, _ = auth.login("u-eng", "agent_engineer", auth.hash_password("pw"), "pw")
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/dashboard/noise-bands:calibrate",
            json={"sut_id": "sut-a4", "evalset_id": "es-a4", "k": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["k"] == 3
        assert body["metrics"]["score"]["sigma"] > 0

        got = await client.get(
            "/api/v1/dashboard/noise-bands",
            params={"evalset_id": "es-a4"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert got.status_code == 200
        items = got.json()["items"]
        score_rows = [i for i in items if i["metric"] == "score"]
        assert score_rows and score_rows[0]["sigma"] > 0
        assert score_rows[0]["delta"] > 0


@pytest.mark.asyncio
async def test_calibrate_endpoint_rejects_service_role(tmp_path: Path) -> None:
    from rsi.api.app import create_app
    from rsi.security.auth import AuthService

    services = _calib_services(tmp_path)
    auth: AuthService = services["auth_service"]
    token, _ = auth.login("u-svc", "service", auth.hash_password("pw"), "pw")
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/dashboard/noise-bands:calibrate",
            json={"sut_id": "sut-a4", "evalset_id": "es-a4"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
