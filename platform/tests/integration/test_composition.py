"""Composition-root integration: default create_app assembly + offline run."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel import SQLModel, select

from rsi.api.app import create_app
from rsi.bootstrap import build_services
from rsi.carrier.adapters.fake import FakeCarrier
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    LinkSpec,
    SshAccess,
    ExpectedAnswer,
    RootCauseExpectation,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.db.tables import Judgment
from rsi.settings import DatabaseSettings, Settings

pytestmark = pytest.mark.asyncio

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
OUT = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown"}'


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path,
        database=DatabaseSettings(url=f"sqlite:///{tmp_path/'comp.db'}"),
    )


async def test_create_app_default_assembly() -> None:
    """create_app() with no services builds the full offline assembly."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/system/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


async def test_full_stack_offline_run(tmp_path: Path) -> None:
    """build_services -> one Fake batch -> judge + tracing pipelines fed."""
    services = build_services(_settings(tmp_path))
    SQLModel.metadata.create_all(services["engine"])
    # route the default adapter to a scripted FakeCarrier
    services["host_service"].adapters["opencode"] = FakeCarrier(
        responses={PROMPT: f"<FINAL_JSON>{OUT}</FINAL_JSON>"}
    )

    env = EnvSpec(
        name="lab-comp",
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
    )
    tasks = [
        TaskRecord(
            task_id="fault/comp-01",
            title="t",
            task=TaskSpec(prompt=PROMPT),
            expected=ExpectedAnswer(
                root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
            ),
        )
    ]
    stats = await services["batch_runner"].run_batch(
        "batch-comp", env, tasks, "cfg-v1", "sut-img", "read-only"
    )
    assert stats.n == 1

    # tracing: collector buffered + finalized the run's events
    collector = services["tracing_collector"]
    assert collector.all_events(), "no trace events collected"
    run_ids = {e.run_id for e in collector.all_events()}
    for run_id in run_ids:
        assert collector.get_meta(run_id) is not None
        assert services["trace_store"].get_meta(run_id) is not None

    # judge pipeline ran end-to-end and persisted a judgment (session_factory)
    with services["session_factory"]() as session:
        rows = session.exec(select(Judgment)).all()
    assert len(rows) == 1
    assert rows[0].run_id in run_ids
