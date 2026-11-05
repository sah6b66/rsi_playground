"""Unit tests: AttributionAnalyzer rule layer (DD-11 §5.1, TU-033)."""

from __future__ import annotations

from datetime import UTC, datetime

from rsi.core.enums import EventType
from rsi.core.models import (
    Efficiency,
    PathQuality,
    SafetyVerdict,
    Scores,
    TraceEvent,
    Verdict,
)
from rsi.optimizer.attribution import AttributionAnalyzer


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _verdict(
    skipped: list[str] | None = None,
    violations: list[str] | None = None,
    rca: float = 0.0,
) -> Verdict:
    return Verdict(
        scores=Scores(
            root_cause_accuracy=rca,
            task_complete=False,
            diagnostic_steps=2,
            missed_check=0,
            false_positive=0,
        ),
        path_quality=PathQuality(
            verdict="suspect",
            skipped_critical_steps=skipped or [],
            reasoning="test",
        ),
        efficiency=Efficiency(tokens=100, tool_calls=3, wall_time_s=1.0),
        safety=SafetyVerdict(violations=violations or []),
    )


def _tool_call(run_id: str, command: str) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut-1",
        seq=0,
        ts=_ts(),
        event_type=EventType.TOOL_CALL,
        data={"args": {"command": command}},
    )


def _run(run_id: str, verdict: Verdict, commands: list[str]) -> dict:
    return {
        "run_id": run_id,
        "task_id": "fault/demo-01",
        "verdict": verdict,
        "events": [_tool_call(run_id, c) for c in commands],
    }


ANALYZER = AttributionAnalyzer()


class TestSingleCluster:
    def test_one_failed_run_produces_one_cluster(self):
        run = _run("run-1", _verdict(skipped=["display bgp peer"]), commands=[])
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        assert len(report.clusters) == 1
        c = report.clusters[0]
        assert c.failure_mode == "missed_critical_command"
        assert c.cluster_size == 1
        assert c.sample_run_ids == ["run-1"]


class TestDefectClass:
    def test_evidence_missing_everywhere_is_class_b(self):
        run = _run(
            "run-1",
            _verdict(skipped=["display bgp peer vpn-instance"]),
            commands=["display clock"],
        )
        report = ANALYZER.analyze(
            [run],
            sut_config={"suts/sut-1/system-prompt.md": "step 1: display clock only"},
            opt_history=[],
        )
        assert report.clusters[0].defect_class == "B"

    def test_evidence_in_prompt_is_class_a(self):
        run = _run(
            "run-1",
            _verdict(skipped=["display bgp peer"]),
            commands=["display clock"],
        )
        report = ANALYZER.analyze(
            [run],
            sut_config={
                "suts/sut-1/system-prompt.md": "step 2: display bgp peer to check"
            },
            opt_history=[],
        )
        assert report.clusters[0].defect_class == "A"

    def test_evidence_executed_is_not_classified_as_missed(self):
        run = _run(
            "run-1",
            _verdict(skipped=["display bgp peer"]),
            commands=["display bgp peer"],
        )
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        # evidence was executed despite the judge flagging it -> not A
        assert report.clusters[0].defect_class == "B"


class TestHistoryFactor:
    def test_repeated_failed_history_downgrades_cluster(self):
        big = [
            _run(f"run-{i}", _verdict(skipped=["display bgp peer"]), commands=[])
            for i in range(1, 4)  # size 3, all-fail history -> 3 * 0.3 = 0.9
        ]
        small = [
            _run("run-10", _verdict(violations=["config command executed"]), commands=[]),
            _run("run-11", _verdict(violations=["config command executed"]), commands=[]),
        ]  # size 2, different mode (hallucinated_command), no history -> 2 * 1.0 = 2.0
        history = [
            {"round": 1, "proposal_type": "modify_prompt",
             "decision": "reverted", "failure_mode": "missed_critical_command",
             "reason": "no gain"},
            {"round": 2, "proposal_type": "add_knowledge",
             "decision": "advise_manual", "failure_mode": "missed_critical_command",
             "reason": "no gain"},
        ]
        report = ANALYZER.analyze(big + small, sut_config={}, opt_history=history)
        # top cluster is the smaller one with no failed history
        assert report.clusters[0].failure_mode == "hallucinated_command"
        assert report.clusters[0].cluster_size == 2
        assert report.clusters[0].sample_run_ids == ["run-10", "run-11"]
        # the downgraded all-fail-history cluster sinks despite being bigger
        assert report.clusters[1].failure_mode == "missed_critical_command"
        assert report.clusters[1].cluster_size == 3

    def test_successful_history_upgrades_cluster(self):
        one = [_run("run-1", _verdict(skipped=["display bgp peer"]), commands=[])]
        other = [
            _run("run-2", _verdict(violations=["config command executed"]), commands=[]),
            _run("run-3", _verdict(violations=["config command executed"]), commands=[]),
        ]
        history = [
            {"round": 1, "proposal_type": "add_runbook",
             "decision": "promoted", "failure_mode": "missed_critical_command",
             "reason": "improved"},
        ]
        report = ANALYZER.analyze(one + other, sut_config={}, opt_history=history)
        # 1 * 1.2 = 1.2 < 2 * 1.0 -> larger cluster still first
        assert report.clusters[0].cluster_size == 2
        # make the successful one bigger -> it must top
        one.append(_run("run-9", _verdict(skipped=["display bgp peer"]), commands=[]))
        report2 = ANALYZER.analyze(one + other, sut_config={}, opt_history=history)
        assert report2.clusters[0].cluster_size == 2
        assert any("display bgp peer" in e for e in report2.clusters[0].evidence)


class TestOtherModes:
    def test_no_skipped_steps_falls_back_mode(self):
        run = _run("run-1", _verdict(rca=0.0), commands=["display clock"])
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        assert report.clusters[0].failure_mode == "causal_misattribution"
