"""F8 tests: D-15 attribution misc cluster + new features + (cmd, seq) refs."""

from __future__ import annotations

from rsi.core.enums import EventType
from rsi.core.models import TaskResult, TraceEvent
from rsi.optimizer.attribution import AttributionAnalyzer

from tests.unit.test_attribution import _run, _tool_call, _verdict, _ts

ANALYZER = AttributionAnalyzer()


def _tool_call_seq(run_id: str, command: str, seq: int) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut-1",
        seq=seq,
        ts=_ts(),
        event_type=EventType.TOOL_CALL,
        data={"args": {"command": command}},
    )


def _knowledge_query(run_id: str, query: str, seq: int) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut-1",
        seq=seq,
        ts=_ts(),
        event_type=EventType.KNOWLEDGE_QUERY,
        data={"query": query},
    )


class TestMiscCluster:
    def test_scatter_runs_merge_into_misc(self):
        # two runs share a failure signature; one run is a singleton scatter
        same = [
            _run(f"run-{i}", _verdict(skipped=["display bgp peer"]), commands=[])
            for i in (1, 2)
        ]
        scatter = [_run("run-9", _verdict(rca=0.0), commands=["display clock"])]
        report = ANALYZER.analyze(same + scatter, sut_config={}, opt_history=[])
        modes = [c.failure_mode for c in report.clusters]
        assert "misc" in modes
        misc = next(c for c in report.clusters if c.failure_mode == "misc")
        assert misc.cluster_size == 1
        assert misc.sample_run_ids == ["run-9"]
        # the >=2 cluster stays independent
        kept = next(c for c in report.clusters if c.failure_mode != "misc")
        assert kept.cluster_size == 2
        # misc sorts last
        assert report.clusters[-1].failure_mode == "misc"

    def test_single_cluster_report_not_misc(self):
        run = _run("run-1", _verdict(skipped=["display bgp peer"]), commands=[])
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        assert len(report.clusters) == 1
        assert report.clusters[0].failure_mode == "missed_critical_command"


class TestNewFeatures:
    def test_knowledge_queries_feature_in_evidence(self):
        run = {
            "run_id": "run-1",
            "task_id": "fault/demo-01",
            "verdict": _verdict(rca=0.0),
            "events": [_knowledge_query("run-1", "vpn runbook", seq=3)],
        }
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        evidence = report.clusters[0].evidence
        assert any(
            e.startswith("knowledge_query=vpn runbook,seq=3") for e in evidence
        )

    def test_final_json_parsed_false_surfaced(self):
        run = {
            "run_id": "run-1",
            "task_id": "fault/demo-01",
            "verdict": _verdict(rca=0.0),
            "result": TaskResult(task_complete=False, parse_error=True),
            "events": [],
        }
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        assert "final_json_parsed=false" in report.clusters[0].evidence

    def test_command_evidence_carries_trace_seq(self):
        run = {
            "run_id": "run-1",
            "task_id": "fault/demo-01",
            "verdict": _verdict(rca=0.0),
            "events": [_tool_call_seq("run-1", "display clock", seq=5)],
        }
        report = ANALYZER.analyze([run], sut_config={}, opt_history=[])
        evidence = report.clusters[0].evidence
        # (command, trace seq) pair shape — D-15
        assert any(e == "cmd=display clock,seq=5" for e in evidence)
