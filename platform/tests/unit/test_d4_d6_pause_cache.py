"""D-4 DbPauseStore (PAUSED 落库) + D-6 config-content cache validation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlmodel import Session, SQLModel, select
from sqlmodel.pool import StaticPool

from rsi.db.tables import Iteration
from rsi.orchestrator.iteration import DbPauseStore, IterationEngine

from tests.unit.test_iteration_engine_full import _engine, _run


def _make_db(tmp_path: Path) -> tuple[Engine, object]:
    engine = create_engine(
        f"sqlite:///{tmp_path/'pause.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine, (lambda: Session(engine, expire_on_commit=False))


def _seed_iteration(session_factory, iteration_id: str, state: str = "ATTRIBUTING"):
    with session_factory() as s:
        s.add(
            Iteration(
                id=iteration_id,
                sut_id="sut-1",
                profile_id="prof-1",
                mode="p1",
                state=state,
                round=0,
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        s.commit()


# -- D-4: DbPauseStore round-trip -------------------------------------------------


class TestDbPauseStore:
    def test_save_get_roundtrip_new_instance(self, tmp_path):
        _, sf = _make_db(tmp_path)
        _seed_iteration(sf, "iter-a")
        DbPauseStore(sf).save_pause("iter-a", "awaiting_review", "2026-01-01T01:00:00")
        # "restart": a brand-new store instance reads the persisted record
        rec = DbPauseStore(sf).get_pause("iter-a")
        assert rec is not None
        assert rec["iteration_id"] == "iter-a"
        assert rec["current_step"] == "awaiting_review"
        assert rec["paused_at"] == "2026-01-01T01:00:00"

    def test_clear_pause(self, tmp_path):
        _, sf = _make_db(tmp_path)
        _seed_iteration(sf, "iter-a", "PAUSED")
        store = DbPauseStore(sf)
        store.save_pause("iter-a", "awaiting_direction", "t1")
        store.clear_pause("iter-a")
        assert DbPauseStore(sf).get_pause("iter-a") is None

    def test_get_pause_missing_row_returns_none(self, tmp_path):
        _, sf = _make_db(tmp_path)
        assert DbPauseStore(sf).get_pause("iter-nope") is None

    def test_list_active_pauses_skips_terminal_states(self, tmp_path):
        _, sf = _make_db(tmp_path)
        _seed_iteration(sf, "iter-live", "PAUSED")
        _seed_iteration(sf, "iter-done", "FINISHED")
        store = DbPauseStore(sf)
        store.save_pause("iter-live", "awaiting_review", "t1")
        store.save_pause("iter-done", "awaiting_review", "t1")
        ids = {r["iteration_id"] for r in store.list_active_pauses()}
        assert ids == {"iter-live"}


# -- D-4: engine with DbPauseStore persists PAUSED across "restart" ---------------


@pytest.mark.asyncio
async def test_manual_gate_engine_with_db_pause_store(tmp_path):
    _, sf = _make_db(tmp_path)
    store = DbPauseStore(sf)
    eng = _engine(enable_manual_gates=True, state_store=store)

    # API side pre-creates the iterations row (as the API router does)
    _seed_iteration(sf, "iter-db-1")

    async def driver():
        return await _run(eng, iteration_id="iter-db-1")

    task = asyncio.create_task(driver())
    feed = {
        "awaiting_direction": lambda: eng.gate_confirm_direction(["c1"]),
        "awaiting_hypothesis": lambda: eng.gate_hypothesis({"summary": "h"}),
        "awaiting_review": lambda: eng.gate_review("pass", []),
        "awaiting_decision": lambda: eng.gate_decision("promote", "ok"),
    }
    seen_steps: list[str] = []
    while not task.done():
        if eng.last_state == "PAUSED" and eng._pending_gate:
            # simulate restart: a *fresh* store reads the persisted record
            rec = DbPauseStore(sf).get_pause("iter-db-1")
            assert rec is not None
            seen_steps.append(rec["current_step"])
            await feed[eng.current_step]()
        await asyncio.sleep(0.01)
    result = await task
    assert result["decision"].action == "PROMOTE"
    assert seen_steps == [
        "awaiting_direction",
        "awaiting_hypothesis",
        "awaiting_review",
        "awaiting_decision",
    ]
    # finish clears the persisted pause record
    assert DbPauseStore(sf).get_pause("iter-db-1") is None


# -- D-6: config-content cache validation -----------------------------------------


pytestmark = pytest.mark.asyncio


class TestConfigContentCache:
    async def _run_once(self, tmp_path, judge, cache, cfg_id):
        from rsi.carrier.adapters.fake import FakeCarrier
        from rsi.carrier.hosts import HostPool
        from rsi.carrier.network import NetPolicy
        from rsi.carrier.service import HostService
        from rsi.core.models import DeviceSpec, Efficiency, EnvSpec, ExpectedAnswer, \
            LinkSpec, PathQuality, PrepareAction, RootCauseExpectation, Scores, \
            SshAccess, SafetyVerdict, TaskRecord, TaskSpec, Topology, Verdict
        from rsi.decision.aggregator import StatsAggregator
        from rsi.orchestrator.batch import BatchRunner
        from rsi.orchestrator.budget import BudgetController
        from rsi.orchestrator.checkpoint import CheckpointStore
        from rsi.orchestrator.event_bus import EventBus
        from rsi.sandbox.backends.fake import FakeSandboxBackend
        from rsi.sandbox.service import SandboxService
        from rsi.tracing.collector import TraceCollector
        from rsi.tracing.seq import SeqAllocator

        prompt = "diagnose and reply FINAL_JSON"
        out = ('working...<FINAL_JSON>{"task_complete": true, '
               '"root_cause": "GE0/0/1 shutdown", "confidence": 0.9}'
               "</FINAL_JSON>")
        env = EnvSpec(
            name="lab-d6",
            backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
            topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R1:GE0/0/2"))]),
            ssh_access={"R1": SshAccess(ip="10.0.0.1", port=2001,
                                        username="a", password="p")},
        )
        task = TaskRecord(
            task_id="fault/d6-01",
            title="t",
            prepare=[PrepareAction(action="exec_cli", entries=[
                {"device": "R1", "cli_lines": ["shutdown"]}])],
            task=TaskSpec(prompt=prompt),
            expected=ExpectedAnswer(
                root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")),
        )
        runner = BatchRunner(
            sandbox=SandboxService(backends={"fake": FakeSandboxBackend()},
                                   labs_dir=tmp_path / "labs"),
            carrier=HostService(
                host_pool=HostPool(), net_policy=NetPolicy(),
                adapters={"fake": FakeCarrier(responses={prompt: out})},
            ),
            collector=TraceCollector(SeqAllocator(), None),
            trace_store=None,
            judge=judge,
            ckpt=CheckpointStore(),
            budget=BudgetController(
                stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
            ),
            event_bus=EventBus(),
            stats_agg=StatsAggregator(),
            result_cache=cache,
        )
        await runner.run_batch("b", env, [task], cfg_id, "sut")

    async def test_inplace_config_dir_change_invalidates(self, tmp_path):
        from tests.unit.test_cache_adr5 import MockJudge

        judge = MockJudge()
        cache: dict = {}
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "system_prompt.md").write_text("v1", encoding="utf-8")

        await self._run_once(tmp_path, judge, cache, str(cfg_dir))
        assert judge.calls == 1
        assert len(cache) == 1
        # in-place mutation -> content hash differs -> miss + overwrite
        (cfg_dir / "system_prompt.md").write_text("v2", encoding="utf-8")
        await self._run_once(tmp_path, judge, cache, str(cfg_dir))
        assert judge.calls == 2
        assert len(cache) == 1  # same key, value overwritten

    async def test_unchanged_config_dir_hits(self, tmp_path):
        from tests.unit.test_cache_adr5 import MockJudge

        judge = MockJudge()
        cache: dict = {}
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "system_prompt.md").write_text("v1", encoding="utf-8")
        await self._run_once(tmp_path, judge, cache, str(cfg_dir))
        await self._run_once(tmp_path, judge, cache, str(cfg_dir))
        assert judge.calls == 1  # hit

    async def test_nonpath_version_id_keeps_string_semantics(self, tmp_path):
        from tests.unit.test_cache_adr5 import MockJudge

        judge = MockJudge()
        cache: dict = {}
        await self._run_once(tmp_path, judge, cache, "cfg-v1")
        await self._run_once(tmp_path, judge, cache, "cfg-v1")
        assert judge.calls == 1  # string-compare hit, no content check
