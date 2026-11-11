"""Audit round-4 group-3 fixes (see .audit4/T1-a.md A-14, T1-d.md D-2, T2.md W-C).

Covers:
- A-14: iteration lifecycle / re-judge endpoints require ``iteration:manage``
  (admin/agent_engineer only — the service automation role gets 403)
- D-2:  per-request disabled check must work under the default assembly,
  where the inline users dict is empty but the auth service has a DB
  session factory (a disabled DB user with a valid JWT -> 401)
- W-C:  run/judgment read endpoints fall back to the DB when the in-memory
  runs store misses (production runner writes only the DB)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


def _services(**extra) -> dict:
    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
            "svc-1": {
                "user_id": "u-svc",
                "role": "service",
                "password_hash": auth.hash_password("svc-pw"),
            },
            "eng-1": {
                "user_id": "u-eng",
                "role": "agent_engineer",
                "password_hash": auth.hash_password("eng-pw"),
            },
        },
    }
    services.update(extra)
    return services


@pytest_asyncio.fixture
async def client():
    app = create_app(_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c._app = app
        yield c


async def _h(client, username, password) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ── A-14: iteration:manage permission ────────────────────────────────────


class TestIterationManage:
    async def test_service_forbidden_on_iteration_lifecycle(self, client):
        h_svc = await _h(client, "svc-1", "svc-pw")
        client._app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
        # create
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": "sut-1", "profile_id": "p-1"},
            headers=h_svc,
        )
        assert resp.status_code == 403, resp.text
        # pause / resume / abort / delete on a (nonexistent) iteration:
        # 403 must win over 404 — RBAC runs before the handler body.
        for suffix in (":pause", ":resume", ":abort"):
            resp = await client.post(
                f"/api/v1/iterations/iter-x{suffix}", headers=h_svc
            )
            assert resp.status_code == 403, (suffix, resp.status_code)
        resp = await client.delete("/api/v1/iterations/iter-x", headers=h_svc)
        assert resp.status_code == 403
        # re-judge
        resp = await client.post("/api/v1/runs/run-x:re-judge", headers=h_svc)
        assert resp.status_code == 403, resp.text

    async def test_agent_engineer_allowed_on_iteration_lifecycle(self, client):
        h_eng = await _h(client, "eng-1", "eng-pw")
        client._app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": "sut-1", "profile_id": "p-1"},
            headers=h_eng,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]
        # pause is a real transition (not 403)
        resp = await client.post(
            f"/api/v1/iterations/{it_id}:pause", headers=h_eng
        )
        assert resp.status_code == 200, resp.text
        # re-judge on an unknown run: 404 (RBAC passed), never 403
        resp = await client.post("/api/v1/runs/run-x:re-judge", headers=h_eng)
        assert resp.status_code == 404, resp.text


# ── D-2: disabled check under the default (empty users dict) assembly ───


class TestDisabledCheckDefaultAssembly:
    async def test_disabled_db_user_with_valid_jwt_rejected(self):
        # StaticPool: FastAPI runs sync dependencies on a threadpool, and a
        # plain sqlite :memory: engine is per-thread — a file-style shared
        # connection mirrors the production assembly.
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        auth = AuthService(secret="test-secret", session_factory=factory)
        from rsi.db.tables import User

        with factory() as session:
            session.add(
                User(
                    id="u-victim",
                    username="victim",
                    role="researcher",
                    password_hash=auth.hash_password("pw"),
                    disabled=1,
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            session.commit()
        token, _ = auth.login("u-victim", "researcher",
                              auth.hash_password("pw"), "pw")
        # default assembly shape: empty inline users dict + session_factory
        services = {
            "auth_service": auth,
            "audit_log": AuditLog(),
            "users": {},
        }
        app = create_app(services)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/suts", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401, resp.text
            # an enabled DB user keeps passing (control)
            with factory() as session:
                session.add(
                    User(
                        id="u-ok",
                        username="ok-user",
                        role="researcher",
                        password_hash=auth.hash_password("pw2"),
                        disabled=0,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
                session.commit()
            token2, _ = auth.login("u-ok", "researcher",
                                   auth.hash_password("pw2"), "pw2")
            resp = await client.get(
                "/api/v1/suts", headers={"Authorization": f"Bearer {token2}"}
            )
            assert resp.status_code == 200, resp.text


# ── W-C: DB-backed run/judgment reads ────────────────────────────────────


class TestRunsReadDbFallback:
    async def test_trigger_then_run_reads_hit_db(self, tmp_path, monkeypatch):
        """Default assembly: trigger a batch, let it finish, then the run
        list/detail/judgment endpoints must read the DB rows the runner
        wrote (the in-memory runs store stays empty)."""
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        from rsi.bootstrap import build_services
        from rsi.db.tables import Run
        from rsi.orchestrator.batch import BatchRunner

        services = build_services()
        # F1 修复后 database 默认落 <RSI_HOME>/platform.db（全新空文件）；
        # 生产建表入口是 python -m rsi.init，build_services 不建表——
        # 测试自足建表，避免依赖 CWD 残留库（曾经的隐性串扰源）。
        from rsi.db import create_engine, init_db
        from rsi.settings import Settings

        init_db(create_engine(Settings().database_url), "admin", "")
        auth: AuthService = services["auth_service"]
        token, _ = auth.login(
            "u-admin", "admin", auth.hash_password("pw"), "pw"
        )
        app = create_app(services)
        h = {"Authorization": f"Bearer {token}"}
        runner: BatchRunner = services["batch_runner"]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
            app.state.stores["evalsets"]["es-1"] = {"id": "es-1"}
            resp = await client.post(
                "/api/v1/batches",
                json={
                    "evalset_id": "es-1", "sut_id": "sut-1",
                    "purpose": "iterate",
                },
                headers=h,
            )
            assert resp.status_code == 201, resp.text
            batch_id = resp.json()["id"]
            if runner._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*runner._tasks, return_exceptions=True),
                    timeout=120,
                )
            # pick a run id the runner persisted
            factory = services["session_factory"]
            with factory() as session:
                run_ids = [
                    r.id
                    for r in session.exec(
                        __import__("sqlmodel", fromlist=["select"]).select(
                            Run
                        ).where(Run.batch_id == batch_id)
                    ).all()
                ]
            assert run_ids, "runner wrote no runs rows"

            # memory runs store is empty (production path) — reads must
            # still resolve via the DB
            assert not app.state.stores["runs"]
            page = await client.get(
                f"/api/v1/batches/{batch_id}/runs", headers=h
            )
            assert page.status_code == 200, page.text
            items = page.json()["items"]
            assert items, "GET /batches/{id}/runs returned no DB rows"
            assert {i["id"] for i in items} >= set(run_ids)

            got = await client.get(f"/api/v1/runs/{run_ids[0]}", headers=h)
            assert got.status_code == 200, got.text
            assert got.json()["id"] == run_ids[0]
            assert got.json()["batch_id"] == batch_id

            # judgment endpoint resolves a DB-backed run (verdict defaults
            # when no judgments row exists — the run lookup is the fix)
            jd = await client.get(
                f"/api/v1/runs/{run_ids[0]}/judgment", headers=h
            )
            assert jd.status_code == 200, jd.text
            assert jd.json()["run_id"] == run_ids[0]

    async def test_memory_store_still_wins(self, client):
        """Test-injected memory runs stay visible (memory-first semantics)."""
        h = await _h(client, "admin", "admin-pw")
        client._app.state.stores["runs"]["run-m1"] = {
            "id": "run-m1",
            "batch_id": "b-x",
            "task_id": "t-1",
            "status": "done",
            "result": {"k": 1},
        }
        got = await client.get("/api/v1/runs/run-m1", headers=h)
        assert got.status_code == 200
        assert got.json()["result"] == {"k": 1}
