"""Unit tests: full IterationEngine hardening (D-3/D-6/T2-3, TU-027).

State-machine legality, real APPLYING wiring (ChangeApplier / DecisionRecorder
/ GitStore promote), hit_rate recalculation, RepairLoop and no-gain
convergence stop, server-side gate methods, restore retry/L2 recovery in
run_task, and judge_run context propagation (T2-5).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rsi.core.errors import InvalidTransitionError
from rsi.core.models import (
    BatchStats,
    Decision,
    DecisionCtx,
    ExpectedAnswer,
    OptimizationProfile,
    RootCauseExpectation,
    TaskRecord,
    TaskSpec,
)
from rsi.decision.recorder import DecisionRecorder
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.iteration import IterationEngine

# -- shared stub rig -----------------------------------------------------------


class ScoredStats(BatchStats):
    """BatchStats subclass carrying per-task scores (task_id -> score)."""

    task_scores: dict[str, float] = {}


def _stats(task_complete: float, rca: float = 0.8, task_scores: dict | None = None):
    stats = ScoredStats(
        metrics={"task_complete": task_complete, "root_cause_accuracy": rca},
        n=2,
        missing=0,
        tokens=100,
        tool_calls=2,
        wall_time_s=1.0,
        tokens_estimated=False,
        task_scores=task_scores or {},
    )
    return stats


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="fault/full-01",
        title="full",
        task=TaskSpec(prompt="p"),
        expected=ExpectedAnswer(root_cause=RootCauseExpectation(expected="x")),
    )


def _profile(**evalsets) -> OptimizationProfile:
    evalsets = evalsets or {"smoke": "", "iterate": "", "holdout": "es-hold"}
    return OptimizationProfile(
        name="full-profile",
        evalsets=evalsets,
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        enabled_targets={"system_prompt"},
    )


class StubRunner:
    def __init__(self, stats: BatchStats) -> None:
        self.stats = stats

    async def run_batch(self, batch_id, env, tasks, config_version_id, image_ref):
        return self.stats


class StubAttribution:
    def analyze(self, failed_runs, sut_config, opt_history):
        from rsi.optimizer.attribution import AttributionAnalyzer

        return AttributionAnalyzer().analyze(failed_runs, sut_config, opt_history)


class StubSelector:
    def decide(self, ctx: DecisionCtx) -> Decision:
        return Decision(action="PROMOTE", rule_hit=5, reason="stub", evidence={})


class StubBudget:
    async def record(self, scope, tokens, cost, wall_s) -> None:
        pass


class StubApplier:
    """ChangeApplier stub returning a fixed candidate version id."""

    def __init__(self, version: str = "cand-123") -> None:
        self.version = version
        self.calls: list[tuple] = []

    def apply(self, sut_id, hypothesis, enabled_targets):
        self.calls.append((sut_id, enabled_targets))
        return self.version


class AlwaysFailCritic:
    def check(self, files, enabled_targets):
        return ["frozen_domain: suts/sut-1/frozen.md"]


def _engine(**kwargs) -> IterationEngine:
    defaults = dict(
        batch_runner=StubRunner(_stats(1.0)),
        attribution_analyzer=StubAttribution(),
        selector=StubSelector(),
        event_bus=EventBus(),
        budget=StubBudget(),
    )
    defaults.update(kwargs)
    return IterationEngine(**defaults)


async def _run(engine: IterationEngine, **kw):
    return await engine.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img", **kw
    )


# -- 1. state machine ----------------------------------------------------------


@pytest.mark.asyncio
async def test_legal_transitions_full_path_passes():
    eng = _engine()
    result = await _run(eng)
    assert eng.last_state == "FINISHED"
    assert result["decision"].action == "PROMOTE"


@pytest.mark.asyncio
async def test_illegal_transitions_raise():
    eng = _engine()
    eng.last_iteration_id = "iter-test"
    eng.round = 1
    for from_, to in [
        ("ATTRIBUTING", "VERIFYING"),  # skips the intermediate states
        ("FINISHED", "ATTRIBUTING"),  # terminal state has no outgoing run edge
        ("HYPOTHESIZING", "FINISHED"),
        ("ARCHIVED", "ATTRIBUTING"),
    ]:
        eng.last_state = from_
        with pytest.raises(InvalidTransitionError):
            await eng._advance("iter-test", from_, to)


@pytest.mark.asyncio
async def test_lifecycle_methods():
    eng = _engine()
    eng.last_iteration_id = "iter-lc"
    eng.last_state = "VERIFYING"
    await eng.pause()
    assert eng.last_state == "PAUSED"
    assert eng.paused_from == "VERIFYING"
    await eng.resume()
    assert eng.last_state == "VERIFYING"
    await eng.abort()
    assert eng.last_state == "ABORTED"
    await eng.archive()
    assert eng.last_state == "ARCHIVED"
    # archive from a running state is illegal
    eng2 = _engine()
    eng2.last_iteration_id = "iter-lc2"
    eng2.last_state = "ATTRIBUTING"
    with pytest.raises(InvalidTransitionError):
        await eng2.archive()
    # start from an active state is illegal; from terminal it works
    with pytest.raises(InvalidTransitionError):
        await eng2.start()
    new_id = await eng.start()
    assert new_id.startswith("iter-")


# -- 2. APPLYING real chain ----------------------------------------------------


@pytest.mark.asyncio
async def test_applying_uses_real_change_applier():
    applier = StubApplier("cand-123")
    eng = _engine(change_applier=applier)
    result = await _run(
        eng,
        hypothesis={
            "summary": "h",
            "model_config_patch": {"temperature": 0.2},
        },
    )
    assert result["candidate_config_version_id"] == "cand-123"
    assert "#cand-r" not in result["candidate_config_version_id"]
    assert applier.calls  # applier really invoked with sut + targets


@pytest.mark.asyncio
async def test_applying_none_applier_keeps_placeholder():
    eng = _engine()
    result = await _run(eng)
    assert result["candidate_config_version_id"] == "cfg-v1#cand-r1"


@pytest.mark.asyncio
async def test_promote_calls_gitstore():
    class StubGit:
        def __init__(self):
            self.promoted = []

        def promote(self, candidate):
            self.promoted.append(candidate)

    from rsi.versioning.changeset import VersionId

    git = StubGit()
    recorder = DecisionRecorder()
    eng = _engine(gitstore=git, decision_recorder=recorder)
    result = await _run(eng)
    assert result["decision"].action == "PROMOTE"
    assert git.promoted == [VersionId("cfg-v1#cand-r1")]
    records = recorder.get(result["iteration_id"])
    assert len(records) == 1
    assert records[0].decision.action == "PROMOTE"


# -- 3. hit_rate recalculation -------------------------------------------------


@pytest.mark.asyncio
async def test_hit_rate_recalculated():
    base = _stats(1.0, rca=0.4, task_scores={"t1": 0.4, "t2": 0.4, "t3": 0.4})
    cand = _stats(1.0, rca=0.6, task_scores={"t1": 0.9, "t2": 0.4, "t3": 0.4})
    eng = _engine(batch_runner=StubRunner(cand))
    result = await _run(
        eng,
        baseline_stats=base,
        hypothesis={
            "summary": "h",
            "predicted_affected_tasks": ["t1", "t2", "t3"],
        },
    )
    assert result["hypothesis"]["hit_rate"] == pytest.approx(1 / 3)
    assert result["hypothesis"]["improved_tasks"] == ["t1"]


# -- 4. RepairLoop -------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_repair_loop_rejects_after_max_rounds():
    eng = _engine(critic=AlwaysFailCritic(), critic_max_repair_rounds=5)
    result = await _run(
        eng,
        hypothesis={
            "summary": "h",
            "changeset_files": [{"path": "suts/sut-1/skills/a.md"}],
        },
    )
    assert result["hypothesis"]["status"] == "rejected"
    assert result["decision"].action == "REVERT"
    assert result["short_circuit"] == "critic_repair_exhausted"
    assert eng.last_state == "FINISHED"


# -- 5. no_gain convergence stop -----------------------------------------------


@pytest.mark.asyncio
async def test_no_gain_convergence_after_three_rounds():
    same = _stats(1.0, rca=0.5)
    eng = _engine(batch_runner=StubRunner(same), no_gain_rounds=3)
    for _ in range(2):
        await _run(eng, baseline_stats=same)
        assert not eng.converged
    result = await _run(eng, baseline_stats=same)
    assert eng.converged
    assert result["converged"] is True


# -- 6. gate methods: state + current_step double check -------------------------


def _paused_engine() -> IterationEngine:
    eng = _engine(enable_manual_gates=True)
    eng.last_iteration_id = "iter-gate"
    eng.round = 1
    eng.last_state = "PAUSED"
    eng.paused_from = "ATTRIBUTING"
    eng.current_step = "awaiting_direction"
    eng._pending_gate = "confirm_direction"
    return eng


@pytest.mark.asyncio
async def test_gate_method_correct_step_passes():
    eng = _paused_engine()
    await eng.gate_confirm_direction(["cluster-1"])
    assert eng._gate_events["confirm_direction"].is_set()
    assert eng._gate_payload["confirm_direction"]["selected"] == ["cluster-1"]


@pytest.mark.asyncio
async def test_gate_method_wrong_step_raises():
    # paused at the direction gate but the decision gate is called
    eng = _paused_engine()
    with pytest.raises(InvalidTransitionError):
        await eng.gate_decision("promote")
    # right gate but not paused
    eng2 = _paused_engine()
    eng2.last_state = "ATTRIBUTING"
    with pytest.raises(InvalidTransitionError):
        await eng2.gate_confirm_direction(["c"])


@pytest.mark.asyncio
async def test_manual_gate_pause_wait_resume_roundtrip():
    """Full manual-gate roundtrip: engine pauses, waits, gate wakes it."""
    eng = _engine(enable_manual_gates=True)

    async def driver():
        return await _run(eng)

    import asyncio as _aio

    task_driver = _aio.create_task(driver())
    gate_feed = {
        "awaiting_direction": lambda: eng.gate_confirm_direction(["c1"]),
        "awaiting_hypothesis": lambda: eng.gate_hypothesis({"summary": "h"}),
        "awaiting_review": lambda: eng.gate_review("pass", []),
        "awaiting_decision": lambda: eng.gate_decision("promote", "ok"),
    }
    # wrong gate first: pause at the direction gate and try the hypothesis gate
    while eng.last_state != "PAUSED":
        assert not task_driver.done(), task_driver.exception()
        await _aio.sleep(0.01)
    assert eng.current_step == "awaiting_direction"
    assert eng.state_store.get_pause(eng.last_iteration_id) is not None
    with pytest.raises(InvalidTransitionError):
        await eng.gate_hypothesis({"summary": "x"})
    # feed each gate as the engine pauses at it
    while not task_driver.done():
        if eng.last_state == "PAUSED" and eng._pending_gate:
            await gate_feed[eng.current_step]()
        await _aio.sleep(0.01)
    result = await task_driver
    assert result["decision"].action == "PROMOTE"
    assert eng.state_store.get_pause(result["iteration_id"]) is None
    assert eng.last_state == "FINISHED"


# -- 8. restore retry / L2 recovery (D-13) -------------------------------------


class _FakeApplied:
    pass


class FlakySandbox:
    """restore_task fails N times then succeeds; reset marks L2 recovery."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.restore_calls = 0
        self.resets = 0

    async def prepare(self, lab, task_id, actions):
        return ["applied"]

    async def restore_task(self, lab, applied):
        self.restore_calls += 1
        if self.restore_calls <= self.fail_times:
            from rsi.core.errors import EnvError

            raise EnvError("flaky restore")

    async def reset(self, lab):
        self.resets += 1


class StubCarrier:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.destroyed = []

    def create_instance(self, **kw):
        return type("Inst", (), {"instance_id": "i1"})()

    def submit_task(self, instance, task):
        return type("Handle", (), {"exit_code": self.exit_code})()

    def collect(self, handle, timeout_s):
        from rsi.carrier.extract import extract_task_result

        return extract_task_result(
            '{"root_cause": "x", "task_complete": true, "actions": []}'
        )

    def destroy(self, instance):
        self.destroyed.append(getattr(instance, "instance_id", "?"))


class StubJudge:
    def __init__(self) -> None:
        self.calls = []

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        self.calls.append(kw)
        from rsi.core.models import Efficiency, PathQuality, SafetyVerdict, Scores, Verdict

        return Verdict(
            scores=Scores(
                root_cause_accuracy=1.0,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", reasoning="ok"),
            efficiency=Efficiency(),
            safety=SafetyVerdict(),
            missing=False,
        )


class StubCollector:
    def ingest(self, events):
        pass

    def finalize(self, run_id):
        pass


class StubAgg:
    def aggregate(self, verdicts):
        from rsi.core.models import BatchStats

        return BatchStats(
            metrics={"task_complete": 1.0, "root_cause_accuracy": 1.0},
            n=len(verdicts),
            missing=0,
            tokens=0,
            tool_calls=0,
            wall_time_s=0.0,
            tokens_estimated=False,
        )


def _batch_runner(sandbox, carrier, judge):
    from rsi.orchestrator.batch import BatchRunner
    from rsi.orchestrator.checkpoint import CheckpointStore

    return BatchRunner(
        sandbox=sandbox,
        carrier=carrier,
        collector=StubCollector(),
        trace_store=None,
        judge=judge,
        ckpt=CheckpointStore(),
        budget=StubBudget(),
        event_bus=EventBus(),
        stats_agg=StubAgg(),
    )


@pytest.mark.asyncio
async def test_restore_retry_succeeds_task_continues():
    sandbox = FlakySandbox(fail_times=2)  # calls 1,2 fail; call 3 succeeds
    runner = _batch_runner(sandbox, StubCarrier(), StubJudge())
    verdicts = []
    for seq in range(2):  # second task proves the batch survived task 1
        result, verdict = await runner.run_task(
            "batch-x",
            _task(),
            type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})(),
            "snap0",
            "cfg-v1",
            "img",
            "read-only",
            seq,
        )
        verdicts.append(verdict)
    assert sandbox.restore_calls >= 3
    assert not verdicts[0].missing


@pytest.mark.asyncio
async def test_restore_always_fails_env_failed_batch_continues():
    sandbox = FlakySandbox(fail_times=10**9)  # never succeeds
    carrier = StubCarrier()
    runner = _batch_runner(sandbox, carrier, StubJudge())
    lab = type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})()
    result, verdict = await runner.run_task(
        "batch-x", _task(), lab, "snap0", "cfg-v1", "img", "read-only", 0
    )
    assert verdict.missing  # env_failed (0-score), not an exception
    assert sandbox.resets == 1  # L2 recovery attempted (reset + restore)
    # batch continues: the next task still runs
    sandbox.fail_times = 0
    result2, verdict2 = await runner.run_task(
        "batch-x", _task(), lab, "snap0", "cfg-v1", "img", "read-only", 1
    )
    assert not verdict2.missing


@pytest.mark.asyncio
async def test_collect_exit_137_marks_instance_crashed():
    from rsi.core.models import TaskResult  # noqa: F401  (returned implicitly)

    sandbox = FlakySandbox(fail_times=0)
    judge = StubJudge()
    runner = _batch_runner(sandbox, StubCarrier(exit_code=137), judge)
    result, verdict = await runner.run_task(
        "batch-x",
        _task(),
        type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})(),
        "snap0",
        "cfg-v1",
        "img",
        "read-only",
        0,
    )
    assert result.instance_crashed is True
    assert verdict.missing
    assert judge.calls == []  # judgment skipped for infra crashes


# -- 9. judge_run context (T2-5) ------------------------------------------------


@pytest.mark.asyncio
async def test_judge_run_receives_config_version_context():
    judge = StubJudge()
    runner = _batch_runner(FlakySandbox(0), StubCarrier(), judge)
    await runner.run_task(
        "verify-holdout-es-hold",
        _task(),
        type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})(),
        "snap0",
        "cfg-v2",
        "img",
        "read-only",
        0,
    )
    assert judge.calls, "judge_run must have been called"
    kw = judge.calls[0]
    assert kw.get("config_version_id") == "cfg-v2"
    assert kw.get("batch_purpose") == "candidate_eval"


class _PromptCapturingLLM:
    async def complete(self, kind, name, *, vars=None, schema=None):
        class R:
            parsed = None

        self.last_vars = dict(vars or {})
        # root_cause schema result
        r = R()
        if name == "root_cause":
            r.parsed = type("P", (), {"match": True, "confidence": 0.9})()
        else:
            r.parsed = type(
                "Q", (), {"verdict": "pass", "skipped_critical_steps": [],
                          "checklist_missed": [], "checklist_false_positive": [],
                          "reasoning": "ok"}
            )()
        return r


@pytest.mark.asyncio
async def test_llm_judge_prompt_contains_config_version():
    from rsi.judge.llm_judge import LLMJudge

    llm = _PromptCapturingLLM()
    judge = LLMJudge(llm_client=llm)
    task = SimpleNamespace(
        task=SimpleNamespace(
            title="t",
            expected=SimpleNamespace(
                root_cause=SimpleNamespace(expected="x"),
                critical_steps=[],
                checklist=[],
            ),
        )
    )
    await judge.judge(
        task,
        SimpleNamespace(root_cause="x", actions=[], confidence=None,
                        task_complete=True),
        [],
        config_version_id="cfg-v9",
        batch_purpose="candidate_eval",
    )
    assert "cfg-v9" in llm.last_vars["eval_context"]
    assert "candidate_eval" in llm.last_vars["eval_context"]
