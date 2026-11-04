"""Unit tests for rsi.core.models domain validators (DD-01 §3.1-3.10)."""

import pytest
from pydantic import ValidationError

from rsi.core.enums import EventType, IterationState
from rsi.core.errors import NamingError, UnknownDeviceError
from rsi.core.models import (
    AttributionCluster,
    AttributionReport,
    BatchStats,
    Budget,
    CallApiParams,
    CheckItem,
    CliEntry,
    ConfigApi,
    CostDelta,
    CriticalStep,
    Decision,
    DecisionCtx,
    DeviceSpec,
    Efficiency,
    EnvSpec,
    EvalTask,
    ExpectedAnswer,
    ExternalDep,
    Goal,
    GuardPolicy,
    Hypothesis,
    IterationSnapshot,
    LLMResponse,
    LLMUsage,
    LabHandle,
    LinkSpec,
    LlmCompare,
    MetricDef,
    NoiseBandInfo,
    OptimizationProfile,
    PathQuality,
    PrepareAction,
    ProblemStatement,
    ReadyCheck,
    RiskItem,
    RootCauseExpectation,
    SafetyVerdict,
    Scores,
    SnapRef,
    SshAccess,
    StateAssert,
    StopPolicy,
    TaskRecord,
    TaskResult,
    TaskSpec,
    Topology,
    TraceEvent,
    Verdict,
    WiretapFrame,
    ActionRecord,
    AppliedAction,
    EvalsetBinding,
    FAILURE_MODES,
)

SSH = SshAccess(ip="10.0.0.1", username="admin", password="test")


def make_env(**over):
    kw = dict(
        name="test-env",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(links=[]),
        ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password="test")},
    )
    kw.update(over)
    return EnvSpec(**kw)


def make_task(**over):
    kw = dict(
        task_id="fault/bgp-down-01",
        title="BGP session down",
        task=TaskSpec(prompt="fix it, FINAL_JSON"),
        expected=ExpectedAnswer(root_cause=RootCauseExpectation(expected="bad peer config")),
        metrics=[MetricDef(name="root_cause_accuracy")],
    )
    kw.update(over)
    return TaskRecord(**kw)


class TestEnvSpec:
    def test_minimal(self):
        env = make_env()
        assert env.devices[0].name == "R1"
        assert env.model_config["frozen"]

    def test_frozen(self):
        env = make_env()
        with pytest.raises(ValidationError):
            env.name = "other"

    def test_ssh_access_must_cover_devices(self):
        with pytest.raises(ValidationError):
            EnvSpec(
                name="test",
                backend="fake",
                devices=[
                    DeviceSpec(name="R1", type="vrp", config="c"),
                    DeviceSpec(name="R2", type="vrp", config="c"),
                ],
                topology={"links": []},
                ssh_access={"R1": SshAccess(ip="10.0.0.1", username="a", password="p")},
            )

    def test_ssh_access_no_extra_keys(self):
        with pytest.raises(ValidationError):
            make_env(
                ssh_access={
                    "R1": SshAccess(ip="10.0.0.1", username="a", password="p"),
                    "GHOST": SshAccess(ip="10.0.0.2", username="a", password="p"),
                }
            )

    def test_duplicate_device_names_rejected(self):
        with pytest.raises(ValidationError):
            make_env(
                devices=[
                    DeviceSpec(name="R1", type="vrp", config="c"),
                    DeviceSpec(name="R1", type="vrp", config="c"),
                ]
            )

    def test_devices_min_length(self):
        with pytest.raises(ValidationError):
            make_env(devices=[])

    def test_site_override_backend_only(self):
        site = make_env(
            backend="pnetlab",
            ssh_access={"R1": SshAccess(ip="10.1.1.1", username="s", password="p")},
        )
        merged = make_env().apply_site_override(site)
        assert merged.backend == "pnetlab"
        assert merged.ssh_access["R1"].ip == "10.1.1.1"

    def test_site_override_conflicting_field_rejected(self):
        site = make_env(name="site-env")
        with pytest.raises(NamingError):
            make_env().apply_site_override(site)


class TestSshAccess:
    def test_password_or_ref_exclusive(self):
        with pytest.raises(ValidationError):
            SshAccess(ip="10.0.0.1", username="a", password="p", password_ref="cred-1")

    def test_neither_allowed(self):
        acc = SshAccess(ip="10.0.0.1", username="a")
        assert acc.password is None and acc.password_ref is None

    def test_port_bounds(self):
        with pytest.raises(ValidationError):
            SshAccess(ip="10.0.0.1", username="a", password="p", port=70000)


class TestDeviceSpec:
    def test_name_pattern(self):
        with pytest.raises(ValidationError):
            DeviceSpec(name="1bad", type="vrp", config="c")

    def test_type_literal(self):
        with pytest.raises(ValidationError):
            DeviceSpec(name="R1", type="juniper", config="c")


class TestReadyCheck:
    def test_cli_match_requires_fields(self):
        with pytest.raises(ValidationError):
            ReadyCheck(type="cli_match")
        ReadyCheck(type="cli_match", device="R1", cli="display clock", expect="08:00")

    def test_ssh_reachable_ok(self):
        ReadyCheck(type="ssh_reachable")


class TestPrepareAction:
    def test_exec_cli(self):
        pa = PrepareAction(
            action="exec_cli",
            entries=[{"device": "R1", "cli_lines": ["int GE0/0/1", "shutdown"]}],
        )
        assert pa.entries[0].device == "R1"

    def test_exec_cli_requires_entries(self):
        with pytest.raises(ValidationError):
            PrepareAction(action="exec_cli")

    def test_call_api(self):
        pa = PrepareAction(
            action="call_api",
            api="cfg-api",
            params={
                "env_name": "test",
                "scenario": "s1",
                "devices": [{"device": "R1", "cli_lines": ["cmd"]}],
            },
        )
        assert pa.params.env_name == "test"

    def test_call_api_requires_api_and_params(self):
        with pytest.raises(ValidationError):
            PrepareAction(action="call_api", api="cfg-api")

    def test_exec_cli_rejects_api(self):
        with pytest.raises(ValidationError):
            PrepareAction(
                action="exec_cli",
                entries=[CliEntry(device="R1", cli_lines=["x"])],
                api="cfg-api",
            )


class TestCliEntry:
    def test_min_lines(self):
        with pytest.raises(ValidationError):
            CliEntry(device="R1", cli_lines=[])


class TestStateAssert:
    def test_requires_at_least_one_expectation(self):
        with pytest.raises(ValidationError):
            StateAssert(device="R1", cli="display clock", expect_contains=[], expect_absent=[])

    def test_contains_ok(self):
        sa = StateAssert(device="R1", cli="display clock", expect_contains=["08:"])
        assert sa.expect_absent == []


class TestTaskRecord:
    def test_task_id_pattern(self):
        with pytest.raises(ValidationError):
            make_task(task_id="bad/id")
        make_task(task_id="inspection/link-check-07")

    def test_metrics_default(self):
        t = make_task()
        assert t.prepare == []


class TestEvalTask:
    def test_resolve_device(self):
        et = EvalTask(env=make_env(), task=make_task())
        assert et.resolve_device("R1").username == "admin"

    def test_resolve_unknown_device(self):
        et = EvalTask(env=make_env(), task=make_task())
        with pytest.raises(UnknownDeviceError):
            et.resolve_device("NOPE")


class TestTraceEvent:
    def test_minimal(self):
        ev = TraceEvent(
            run_id="run-1",
            task_id="fault/bgp-down-01",
            lab_id="lab-1",
            sut_id="sut-a",
            seq=0,
            ts="2026-11-04T00:00:00.000Z",
            event_type=EventType.TOOL_CALL,
        )
        assert ev.late is False

    def test_seq_ge_zero(self):
        with pytest.raises(ValidationError):
            TraceEvent(
                run_id="r",
                task_id="fault/bgp-down-01",
                lab_id="l",
                sut_id="s",
                seq=-1,
                ts="t",
                event_type=EventType.ERROR,
            )


class TestVerdictModels:
    def test_scores_bounds(self):
        with pytest.raises(ValidationError):
            Scores(
                root_cause_accuracy=1.5, task_complete=True, diagnostic_steps=0,
                missed_check=0, false_positive=0,
            )
        s = Scores(
            root_cause_accuracy=0.9, task_complete=True, diagnostic_steps=2,
            missed_check=0, false_positive=0,
        )
        assert s.indeterminate is False

    def test_efficiency_estimated_flag(self):
        e = Efficiency(tokens=1000, estimated=True)
        assert e.estimated

    def test_verdict(self):
        v = Verdict(
            scores=Scores(
                root_cause_accuracy=1.0, task_complete=True, diagnostic_steps=1,
                missed_check=0, false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", reasoning="ok"),
            efficiency=Efficiency(),
            safety=SafetyVerdict(),
        )
        assert v.missing is False
        assert v.reward_hacking_suspect is False


class TestHypothesisModels:
    def test_problem_statement_defaults(self):
        p = ProblemStatement(
            cluster_id="c1",
            failure_mode="reasoning_skip",
            defect_class="A",
            evidence_trace_ids=["run-1"],
            suspected_elements=["prompts/system.md"],
            problem_text="skips verification",
        )
        assert p.evidence_sut_refs == []

    def test_hypothesis(self):
        h = Hypothesis(
            problem_trace_ids=["run-1"],
            failure_type="call_order_error",
            defect_class="A",
            proposal_type="modify_prompt",
            diff_content="+ improve",
            expected_improve="better root cause",
            risk="low",
            verification_task_ids=["fault/bgp-down-01"],
            predicted_affected_tasks=["fault/bgp-down-01"],
        )
        assert h.model_config_patch is None

    def test_hypothesis_diff_exclusive(self):
        with pytest.raises(ValidationError):
            Hypothesis(
                problem_trace_ids=["run-1"],
                failure_type="other",
                defect_class="B",
                proposal_type="custom",
                diff_content="x",
                model_config_patch={"model": "m"},
                expected_improve="y",
                risk="high",
                verification_task_ids=[],
                predicted_affected_tasks=[],
            )

    def test_attribution_report(self):
        r = AttributionReport(
            clusters=[
                AttributionCluster(
                    failure_mode="hallucinated_command",
                    defect_class="B",
                    cluster_size=3,
                    sample_run_ids=["run-1"],
                    evidence=["run-1#L10"],
                )
            ]
        )
        assert r.clusters[0].cluster_size == 3


class TestOptimizationProfile:
    def test_full(self):
        p = OptimizationProfile(
            name="p1",
            evalsets={"iterate": "n9-iterate", "holdout": "n9-holdout", "smoke": "n9-smoke"},
            goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
            enabled_targets={"system_prompt", "skills"},
        )
        assert p.budget is None
        assert p.guards.missed_check_worsen == "reject"

    def test_goals_min_length(self):
        with pytest.raises(ValidationError):
            OptimizationProfile(
                name="p1",
                evalsets={
                    "iterate": "a-iterate",
                    "holdout": "a-holdout",
                    "smoke": "a-smoke",
                },
                goals=[],
                enabled_targets={"skills"},
            )

    def test_targets_min_length(self):
        with pytest.raises(ValidationError):
            OptimizationProfile(
                name="p1",
                evalsets={
                    "iterate": "a-iterate",
                    "holdout": "a-holdout",
                    "smoke": "a-smoke",
                },
                goals=[Goal(metric="m", direction="max", weight=1)],
                enabled_targets=set(),
            )

    def test_budget_positive(self):
        with pytest.raises(ValidationError):
            Budget(max_tokens=0, max_wall_hours=1.0)


class TestTaskResult:
    def test_flags(self):
        tr = TaskResult(task_complete=False, parse_error=True)
        assert tr.parse_error and not tr.instance_crashed
        assert tr.actions == []

    def test_action_record(self):
        assert ActionRecord(action="display bgp peer").detail is None


class TestLLMModels:
    def test_usage_defaults(self):
        assert LLMUsage().estimated is False

    def test_response(self):
        r = LLMResponse(content="ok", model="gpt-x", prompt_version="v1")
        assert r.parsed is None
        assert r.usage.output_tokens == 0


class TestSandboxModels:
    def test_lab_handle(self):
        h = LabHandle(lab_id="lab-1", host="h1", backend="fake", endpoints={"R1": SSH})
        assert h.endpoints["R1"].port == 22

    def test_snap_ref(self):
        s = SnapRef(snapshot_id="snap-1", impl_kind="native", backend_ref=None)
        assert s.impl_kind == "native"


class TestDecisionModels:
    def test_decision(self):
        d = Decision(action="PROMOTE", rule_hit=1, reason="gain", evidence={"delta": 0.1})
        assert d.action == "PROMOTE"

    def test_batch_stats(self):
        b = BatchStats(
            metrics={"root_cause_accuracy": 0.8},
            n=10,
            missing=1,
            tokens=100,
            tool_calls=20,
            wall_time_s=5.0,
            tokens_estimated=False,
        )
        assert b.missing == 1

    def test_cost_delta_and_noise(self):
        CostDelta(tokens=10, tool_calls=1, wall_time_s=0.5, cost_usd=None)
        NoiseBandInfo(metrics={}, n_pairs=4)

    def test_decision_ctx(self):
        DecisionCtx(
            baseline=BatchStats(
                metrics={}, n=5, missing=0, tokens=1, tool_calls=1,
                wall_time_s=1.0, tokens_estimated=False,
            ),
            candidate=BatchStats(
                metrics={}, n=5, missing=0, tokens=1, tool_calls=1,
                wall_time_s=1.0, tokens_estimated=False,
            ),
            noise_bands={},
            high_risk=False,
        )


class TestIterationSnapshot:
    def test_snapshot(self):
        s = IterationSnapshot(
            id="iter-1",
            sut_id="sut-a",
            mode="p1",
            state=IterationState.VERIFYING,
            current_step="verify",
            round=3,
            experiments=["exp-1"],
            budget_used={"tokens": 100, "cost_usd": 0.5, "wall_time_s": 10.0},
        )
        assert s.state is IterationState.VERIFYING


class TestWiretapFrame:
    def test_frame(self):
        f = WiretapFrame(carrier="opencode", kind="assembled", raw={"x": 1}, at="t1")
        assert f.raw == {"x": 1}


class TestAppliedAction:
    def test_minimal(self):
        a = AppliedAction(action="exec_cli", applied_at="2026-11-04T00:00:00Z")
        assert a.backup_files == {}
