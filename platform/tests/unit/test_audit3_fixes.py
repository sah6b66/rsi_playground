"""Audit round-3 fixes (see .audit3/T1-c.md and T2.md).

Covers:
- E1/N1: freeze_check CRLF-normalized hashing (Windows autocrlf checkouts)
- N2:   blind judge — LLM prompts must not leak the eval arm
        (baseline_eval/candidate_eval/config_version_id); provenance is
        persisted on the Judgment row instead
- W2:   API trigger completion callback syncs the in-memory batch store
- W3:   concurrent trigger() calls get isolated per-batch runner copies
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session, select

from rsi.orchestrator.batch import BatchRunner

pytestmark = pytest.mark.asyncio


# ── E1/N1: CRLF-normalized freeze hashing ────────────────────────────────


class TestFreezeCrlfNormalization:
    def test_verify_passes_with_crlf_checkout(self, tmp_path: Path):
        """A CRLF materialization of the frozen files must still verify
        against a manifest computed from the LF bytes (Windows autocrlf)."""
        from rsi.judge.rules import freeze_check

        root = tmp_path / "judge"
        (root / "rules").mkdir(parents=True)
        (root / "prompts").mkdir(parents=True)
        # write LF versions, hash them into a manifest ...
        lf_files = {
            "rules/safety_rules.yaml": "a: 1\nb: 2\n",
            "prompts/root_cause.v1.md": "line1\nline2\n",
        }
        for rel, text in lf_files.items():
            (root / rel).write_bytes(text.encode("utf-8"))
        manifest = freeze_check.write_manifest(root=root)
        assert manifest
        assert freeze_check.verify(manifest_path=root / "rules" / "freeze.manifest", root=root)
        # ... then re-materialize the same content with CRLF: verify must
        # still PASS (hash computed after CRLF->LF normalization).
        for rel, text in lf_files.items():
            (root / rel).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        assert freeze_check.verify(manifest_path=root / "rules" / "freeze.manifest", root=root)

    def test_write_manifest_and_verify_share_normalization(self, tmp_path: Path):
        """write_manifest on CRLF files and verify on the same CRLF files
        agree (both go through the same _normalized_sha256)."""
        from rsi.judge.rules import freeze_check

        root = tmp_path
        (root / "rules").mkdir(parents=True)
        (root / "prompts").mkdir()
        (root / "rules" / "safety_rules.yaml").write_bytes(b"x: 1\r\ny: 2\r\n")
        manifest = freeze_check.write_manifest(root=root)
        assert freeze_check.verify(manifest_path=root / "rules" / "freeze.manifest", root=root)

    def test_repo_manifest_verifies(self):
        from rsi.judge.rules.freeze_check import verify

        assert verify() is True

    def test_content_change_still_fails(self, tmp_path: Path):
        from rsi.judge.rules import freeze_check

        root = tmp_path
        (root / "rules").mkdir(parents=True)
        (root / "prompts").mkdir()
        f = root / "rules" / "safety_rules.yaml"
        f.write_bytes(b"a: 1\n")
        freeze_check.write_manifest(root=root)
        f.write_bytes(b"a: 2\n")  # real tamper, not just line endings
        assert not freeze_check.verify(
            manifest_path=root / "rules" / "freeze.manifest", root=root
        )


# ── N2: blind judge prompts ──────────────────────────────────────────────


class _PromptCapturingLLM:
    def __init__(self) -> None:
        self.prompts: list[tuple[str, dict]] = []

    async def complete(self, component, name, *, vars=None, schema=None):
        self.prompts.append((name, dict(vars or {})))

        class _R:
            parsed = None

        r = _R()
        if name == "root_cause":
            r.parsed = type("P", (), {"match": True, "confidence": 0.9})()
        else:
            r.parsed = type(
                "Q", (), {"verdict": "pass", "skipped_critical_steps": [],
                          "checklist_missed": [], "checklist_false_positive": [],
                          "reasoning": "ok"}
            )()
        return r


def _eval_task():
    from types import SimpleNamespace

    return SimpleNamespace(
        task=SimpleNamespace(
            title="t",
            expected=SimpleNamespace(
                root_cause=SimpleNamespace(expected="x"),
                final_state=[],
                critical_steps=[],
                checklist=[],
            ),
        )
    )


class TestBlindJudgePrompt:
    async def test_prompt_has_no_arm_labels(self):
        from rsi.judge.llm_judge import LLMJudge

        llm = _PromptCapturingLLM()
        judge = LLMJudge(llm_client=llm)
        await judge.judge(
            _eval_task(),
            type(
                "R", (), {"root_cause": "x", "actions": [], "confidence": None,
                          "task_complete": True}
            )(),
            [],
            config_version_id="cfg-v9",
            batch_purpose="candidate_eval",
        )
        assert llm.prompts, "judge must have issued LLM calls"
        for name, vars_ in llm.prompts:
            blob = json.dumps(vars_, ensure_ascii=False)
            assert "eval_context" not in vars_, f"{name}: eval_context var leaked"
            for leak in ("baseline_eval", "candidate_eval", "cfg-v9",
                         "batch_purpose", "config_version"):
                assert leak not in blob, f"{name}: prompt var leaks {leak!r}"

    async def test_prompt_render_has_no_context_placeholder(self):
        """The frozen prompt templates themselves must not render any
        evaluation-context line (blind eval, audit3 N2)."""
        from rsi.llm.prompts import PromptLibrary

        lib = PromptLibrary(
            template_dir=Path(__file__).resolve().parents[2] / "rsi" / "judge" / "prompts",
            component="judge",
        )
        for name, vars_ in (
            ("root_cause", {"task_title": "t", "expected": "e", "actual": "a",
                            "confidence_of_sut": "0.5", "match_hints": "h"}),
            ("path_quality", {"steps_summary": "s", "critical_steps": "c",
                              "checklist_items": "ci", "sut_actions": "a",
                              "task_complete": "True", "actions": "a"}),
        ):
            out = lib.render("judge", name, vars_)
            assert "Evaluation context" not in out

    async def test_judgment_row_persists_eval_context(self, tmp_path):
        """judge_run(config_version_id, batch_purpose) survives as provenance
        on the Judgment row (traceability) even though it never enters the
        prompt."""
        from rsi.core.enums import EventType
        from rsi.core.models import TraceEvent
        from rsi.db.tables import Judgment
        from rsi.judge.aggregate import VerdictAggregator
        from rsi.judge.antihack import AntiHackDetector
        from rsi.judge.api import JudgeService
        from rsi.judge.deterministic import DeterministicJudge
        from rsi.judge.llm_judge import LLMJudge
        from rsi.judge.safety import SafetyScanner

        engine = create_engine(f"sqlite:///{tmp_path}/judge.db")
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        llm = _PromptCapturingLLM()
        svc = JudgeService(
            det=DeterministicJudge(timeout_s=1.0),
            safety=SafetyScanner(),
            llm=LLMJudge(llm_client=llm),
            antihack=AntiHackDetector(),
            agg=VerdictAggregator(),
            session_factory=factory,
        )
        task = _eval_task()
        events = [
            TraceEvent(
                event_type=EventType.TOOL_CALL,
                run_id="run-1",
                seq=0,
                ts="2026-11-05T12:00:00+00:00",
                task_id="fault/t-00",
                lab_id="lab-1",
                sut_id="sut-x",
                data={"cli": "display version"},
            )
        ]
        await svc.judge_run(
            task,
            type("R", (), {"root_cause": "x", "actions": [], "confidence": None,
                           "task_complete": True, "instance_crashed": False})(),
            events,
            type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})(),
            sandbox=None,
            permission="read-only",
            config_version_id="cfg-v9",
            batch_purpose="candidate_eval",
        )
        with factory() as s:
            rows = s.exec(select(Judgment)).all()
        assert len(rows) == 1
        ctx = json.loads(rows[0].eval_context_json)
        assert ctx["config_version_id"] == "cfg-v9"
        assert ctx["batch_purpose"] == "candidate_eval"
        # and the LLM never saw either value
        for name, vars_ in llm.prompts:
            blob = json.dumps(vars_, ensure_ascii=False)
            assert "cfg-v9" not in blob and "candidate_eval" not in blob


# ── W2: trigger on_update syncs the in-memory store ──────────────────────


def _bare_runner() -> BatchRunner:
    return BatchRunner(
        sandbox=None, carrier=None, collector=None, trace_store=None,
        judge=None, ckpt=None, budget=None, event_bus=None, stats_agg=None,
    )


class _Stats:
    n = 1


class TestTriggerOnUpdate:
    async def test_on_update_called_on_success(self, monkeypatch):
        runner = _bare_runner()

        # Patch at class level: trigger() runs the batch on a shallow COPY,
        # so the stub must mutate whichever instance it is called on.
        async def fake_run_batch(self, *a, **kw):
            self.last_status = "finalized"
            return _Stats()

        monkeypatch.setattr(BatchRunner, "run_batch", fake_run_batch)
        batch = {"id": "b1", "evalset_id": "es", "sut_id": "sut"}
        updates: list[dict] = []
        runner.trigger(batch, on_update=updates.append)
        await asyncio.wait_for(asyncio.gather(*runner._tasks), timeout=5)
        assert len(updates) == 1
        assert updates[0]["status"] == "finalized"
        assert updates[0]["progress"]["done"] == 1
        assert updates[0]["finished_at"]

    async def test_on_update_called_on_failure(self):
        runner = _bare_runner()

        async def boom(*a, **kw):
            raise RuntimeError("sandbox exploded")

        runner.run_batch = boom  # type: ignore
        updates: list[dict] = []
        r = runner.trigger({"id": "b-bad"}, on_update=updates.append)
        await asyncio.wait_for(asyncio.gather(*r._tasks), timeout=5)
        assert updates == [
            {
                "status": "failed",
                "progress": {"done": 0, "total": 0},
                "finished_at": updates[0]["finished_at"],
            }
        ]
        assert r.last_status == "failed"

    async def test_broken_callback_is_contained(self):
        runner = _bare_runner()

        async def fake_run_batch(*a, **kw):
            return _Stats()

        runner.run_batch = fake_run_batch  # type: ignore

        def bad_cb(upd):
            raise ValueError("store gone")

        r = runner.trigger({"id": "b1"}, on_update=bad_cb)
        # must complete without raising
        await asyncio.wait_for(asyncio.gather(*r._tasks), timeout=5)
        assert r.last_status in ("finalized", "created")

    async def test_default_assembly_store_synced_after_trigger(self, tmp_path, monkeypatch):
        """W2 end-to-end: with the real build_services() assembly, POST
        /batches followed by background completion leaves the in-memory
        batch store at a terminal status (not 'created' forever)."""
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        from httpx import ASGITransport, AsyncClient

        from rsi.api.app import create_app
        from rsi.bootstrap import build_services
        from rsi.security.auth import AuthService

        services = build_services()
        auth: AuthService = services["auth_service"]
        token, _ = auth.login("u-admin", "admin", auth.hash_password("pw"), "pw")
        app = create_app(services)
        runner: BatchRunner = services["batch_runner"]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
            app.state.stores["evalsets"]["es-1"] = {"id": "es-1"}
            resp = await client.post(
                "/api/v1/batches",
                json={"evalset_id": "es-1", "sut_id": "sut-1", "purpose": "iterate"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 201, resp.text
            batch_id = resp.json()["id"]
            if runner._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*runner._tasks, return_exceptions=True),
                    timeout=60,
                )
            got = await client.get(
                f"/api/v1/batches/{batch_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert got.status_code == 200
            body = got.json()
            assert body["status"] == "finalized", body
            assert body["progress"]["done"] >= 1


# ── W3: concurrent trigger isolation ─────────────────────────────────────


class _SlowSandbox:
    """Fake sandbox whose build blocks until released — keeps two triggered
    batches overlapping in time."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def build(self, env, cred=None):
        await asyncio.wait_for(self.gate.wait(), timeout=10)
        return type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})()

    async def destroy(self, lab):
        return None

    async def snapshot(self, lab):
        return type("Snap", (), {"snapshot_id": "s0"})()

    async def restore(self, lab, snap):
        return None


class TestTriggerIsolation:
    async def test_concurrent_triggers_do_not_cross_write(self):
        from rsi.core.models import (
            DeviceSpec, EnvSpec, ExpectedAnswer, SshAccess, TaskRecord, TaskSpec, Topology,
        )
        from rsi.judge.rules import freeze_check  # noqa: F401  (env sanity)
        from rsi.orchestrator.batch import BatchRunner

        sandbox = _SlowSandbox()

        runner = BatchRunner(
            sandbox=sandbox,
            carrier=None,
            collector=None,
            trace_store=None,
            judge=None,
            ckpt=None,
            budget=None,
            event_bus=None,
            stats_agg=None,
        )
        env = EnvSpec(
            name="lab-x",
            backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="c")],
            topology=Topology(),
            ssh_access={
                "R1": SshAccess(ip="10.0.0.1", port=2001, username="a", password="p")
            },
        )
        task = TaskRecord(
            task_id="fault/t-00",
            title="t",
            prepare=[],
            task=TaskSpec(prompt="p"),
            expected=ExpectedAnswer(),
        )

        def resolver(batch):
            return env, [task], "", "sut"

        runner.batch_resolver = resolver

        # two concurrent batches, both gated inside sandbox.build
        r1 = runner.trigger({"id": "batch-aa"})
        r2 = runner.trigger({"id": "batch-bb"})
        await asyncio.sleep(0.05)
        sandbox.gate.set()
        await asyncio.wait_for(
            asyncio.gather(*r1._tasks, *r2._tasks, return_exceptions=True),
            timeout=30,
        )
        # per-copy isolation: last_run_ids keyed per batch contain only their
        # own task entries, and last_status reached a terminal value
        assert set(r1.last_run_ids) <= {"fault/t-00"}
        assert set(r2.last_run_ids) <= {"fault/t-00"}
        assert r1.last_status in ("finalized", "aborted", "failed")
        assert r2.last_status in ("finalized", "aborted", "failed")
        assert r1 is not r2 and r1 is not runner
        # the template runner itself was never mutated
        assert runner.last_run_ids == {}

    async def test_concurrent_triggers_isolate_shared_task_ids(self):
        """Two batches running the SAME task id concurrently must each record
        their own run id without cross-writes (the parallel.py bug class)."""
        sandbox = _SlowSandbox()
        runner = BatchRunner(
            sandbox=sandbox, carrier=None, collector=None, trace_store=None,
            judge=None, ckpt=None, budget=None, event_bus=None, stats_agg=None,
        )

        # drive run_batch directly on copies: fake run_batch writes into
        # self.last_run_ids like the real one does
        orig_run_batch = BatchRunner.run_batch

        async def fake_run_batch(self, batch_id, *a, **kw):
            await asyncio.sleep(0.01)
            self.last_run_ids["shared-task"] = f"run-{batch_id}"
            self.last_status = "finalized"
            return _Stats()

        runner.__class__.run_batch = fake_run_batch  # type: ignore
        try:
            r1 = runner.trigger({"id": "batch-aa"})
            r2 = runner.trigger({"id": "batch-bb"})
            await asyncio.wait_for(
                asyncio.gather(*r1._tasks, *r2._tasks, return_exceptions=True),
                timeout=10,
            )
        finally:
            runner.__class__.run_batch = orig_run_batch  # type: ignore
        assert r1.last_run_ids == {"shared-task": "run-batch-aa"}
        assert r2.last_run_ids == {"shared-task": "run-batch-bb"}


# ── F-2: instance destroyed on every run_task exit path ──────────────────


class _DestroyCountingCarrier:
    """Wraps HostService.destroy to count calls per instance."""

    def __init__(self, carrier) -> None:
        self._carrier = carrier
        self.calls: list[str] = []

    def wrap(self):
        orig = self._carrier.destroy

        def destroy(instance):
            self.calls.append(instance.instance_id)
            return orig(instance)

        self._carrier.destroy = destroy


class TestInstanceDestroyAllPaths:
    async def test_destroy_called_once_on_success(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _task, _env

        rig = Rig(tmp_path)
        counter = _DestroyCountingCarrier(rig.carrier)
        counter.wrap()
        await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        assert len(counter.calls) == 1
        assert rig.carrier._instances == {}

    async def test_destroy_called_once_on_instance_crashed(self, tmp_path):
        from rsi.core.models import TaskResult
        from tests.unit.test_batch_runner import Rig, _task, _env

        rig = Rig(tmp_path)
        counter = _DestroyCountingCarrier(rig.carrier)
        counter.wrap()
        orig_collect = rig.carrier.collect
        rig.carrier.collect = (
            lambda handle, timeout: TaskResult(
                task_complete=False, instance_crashed=True
            )
        )
        try:
            stats = await rig.runner.run_batch(
                "batch-1", _env(), [_task()], "cfg-v1", "sut-img"
            )
        finally:
            rig.carrier.collect = orig_collect
        assert stats.missing == 1  # env_failed verdict, batch survived
        assert len(counter.calls) == 1
        assert rig.carrier._instances == {}

    async def test_destroy_called_once_when_judge_raises(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _task, _env

        rig = Rig(tmp_path)
        counter = _DestroyCountingCarrier(rig.carrier)
        counter.wrap()

        async def boom(*a, **kw):
            raise RuntimeError("judge exploded")

        orig_judge = rig.judge.judge_run
        rig.judge.judge_run = boom
        try:
            with pytest.raises(RuntimeError):
                await rig.runner.run_batch(
                    "batch-1", _env(), [_task()], "cfg-v1", "sut-img"
                )
        finally:
            rig.judge.judge_run = orig_judge
        # judge exceptions still propagate (batch aborts), but the instance
        # handle must not leak.
        assert len(counter.calls) == 1
        assert rig.carrier._instances == {}


# ── F-3: _LoopRunner bridge timeout covers device read timeout, raises EnvError ──


class TestLoopRunnerBridgeTimeout:
    def test_timeout_raises_enverror_not_builtin(self):
        from rsi.core.errors import EnvError
        from rsi.sandbox.service import _LoopRunner

        runner = _LoopRunner(timeout_s=0.05)
        with pytest.raises(EnvError) as ei:
            runner.run(asyncio.sleep(5))
        assert "did not finish" in str(ei.value)
        assert ei.value.context["timeout_s"] == 0.05

    def test_default_timeout_covers_device_read_timeout(self):
        # DeviceSession.exec_cli read_timeout defaults to 120s; the bridge
        # wait must not fire first (F-3).
        from rsi.sandbox.service import _LoopRunner

        assert _LoopRunner.DEFAULT_TIMEOUT_S >= 120

    async def test_escaped_timeout_degrades_task_not_batch(self, tmp_path):
        """A bridge timeout surfacing through sandbox.prepare must be caught
        by run_task's EnvError handling (env_failed verdict, batch continues
        to the next task instead of dying with a bare TimeoutError)."""
        from rsi.core.errors import EnvError
        from rsi.sandbox.service import _LoopRunner
        from tests.unit.test_batch_runner import Rig, _env, _task

        runner = _LoopRunner(timeout_s=0.05)
        rig = Rig(tmp_path)
        orig_prepare = rig.sandbox.prepare

        async def prepare_timeout(lab, task_id, actions):
            try:
                # emulate _run_sync bridging a 5s-hung coroutine: raises
                # EnvError exactly like _LoopRunner.run does on timeout
                runner.run(asyncio.sleep(5))
            except EnvError:
                raise
            return await orig_prepare(lab, task_id, actions)

        rig.sandbox.prepare = prepare_timeout
        tasks = [_task(1), _task(2)]
        stats = await rig.runner.run_batch("batch-1", _env(), tasks, "cfg-v1", "sut-img")
        assert stats.n == 2
        assert stats.missing == 2  # both tasks degrade to env_failed
        assert rig.runner.last_status == "finalized"  # batch reached a terminal state
