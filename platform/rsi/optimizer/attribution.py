"""AttributionAnalyzer — P0 rule-layer failure attribution (DD-11 §5.1).

Three conceptual layers; P0 implements the two rule layers
(FeatureExtractor + PreClusterer) with no LLM calls:

1. per-run features: executed commands (tool_call events), safety errors,
   skipped critical steps, root-cause accuracy;
2. SUT-config cross-reference (v2.10): every skipped critical-step evidence
   that is absent from the executed commands but present in the SUT config
   (system-prompt / skills) is a class-A defect (指引已有未执行); evidence
   absent from both is class B (指引缺失 -> optimize the guidance);
3. clustering by failure signature + opt_history priority factor
   (all-failed history: 0.3, success history: 1.2, none: 1.0).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from rsi.core.enums import EventType
from rsi.core.models import AttributionCluster, AttributionReport, TraceEvent
from rsi.log import get_logger

__all__ = ["AttributionAnalyzer"]

logger = get_logger(__name__)

_FAILED_DECISIONS = frozenset({"reverted", "advise_manual", "rejected"})
_SUCCESS_DECISIONS = frozenset({"promoted", "promote", "accepted"})

_FACTOR_ALL_FAILED = 0.3
_FACTOR_SUCCESS = 1.2
_FACTOR_NONE = 1.0


def _event_fields(event: Any) -> tuple[str, dict[str, Any]]:
    """Normalize a TraceEvent or a plain dict into (event_type, data)."""
    if isinstance(event, TraceEvent):
        return str(event.event_type), dict(event.data)
    data = dict(event.get("data") or {})
    return str(event.get("event_type", "")), data


class _RunFeatures:
    __slots__ = ("run_id", "commands", "errors", "skipped", "root_cause_accuracy")

    def __init__(self, run: dict[str, Any]) -> None:
        self.run_id: str = run["run_id"]
        self.commands: list[str] = []
        for ev in run.get("events") or []:
            etype, data = _event_fields(ev)
            if etype != EventType.TOOL_CALL:
                continue
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            cmd = data.get("cli") or data.get("command") or args.get("cli") or args.get("command")
            if isinstance(cmd, str) and cmd.strip():
                self.commands.append(cmd)
        verdict = run.get("verdict")
        self.errors: list[str] = list(getattr(verdict, "safety", None) and verdict.safety.violations or [])
        pq = getattr(verdict, "path_quality", None)
        self.skipped: list[str] = list(pq.skipped_critical_steps) if pq else []
        scores = getattr(verdict, "scores", None)
        self.root_cause_accuracy: float = (
            float(scores.root_cause_accuracy) if scores else 0.0
        )


class AttributionAnalyzer:
    """Rule-layer attribution over pre-computed failed-run data (P0)."""

    def analyze(
        self,
        failed_runs: list[dict],
        sut_config: dict[str, str],
        opt_history: list[dict],
    ) -> AttributionReport:
        logger.debug(
            "analyze.entry",
            failed_runs=len(failed_runs),
            config_files=len(sut_config),
            opt_history=len(opt_history),
        )
        # Layer 1+2: features and per-run (failure_mode, signature, defect class)
        classified: list[tuple[_RunFeatures, str, tuple[str, ...], str, list[str]]] = []
        for run in failed_runs:
            feats = _RunFeatures(run)
            mode, signature, defect, evidence_refs = self._classify(feats, sut_config)
            classified.append((feats, mode, signature, defect, evidence_refs))

        # Layer 3a: cluster by (failure_mode, signature)
        groups: dict[tuple[str, tuple[str, ...]], list[_RunFeatures]] = defaultdict(list)
        defect_of: dict[tuple[str, tuple[str, ...]], str] = {}
        evidence_of: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
        for feats, mode, signature, defect, refs in classified:
            key = (mode, signature)
            groups[key].append(feats)
            defect_of.setdefault(key, defect)
            for ref in refs:
                if ref not in evidence_of[key]:
                    evidence_of[key].append(ref)

        # Layer 3b: opt_history factor + sort by size x factor desc
        def factor(mode: str) -> float:
            entries = [h for h in opt_history if h.get("failure_mode") == mode]
            if not entries:
                return _FACTOR_NONE
            decisions = {str(h.get("decision", "")).lower() for h in entries}
            if decisions & _SUCCESS_DECISIONS:
                return _FACTOR_SUCCESS
            if decisions & _FAILED_DECISIONS:
                return _FACTOR_ALL_FAILED
            return _FACTOR_NONE

        clusters = [
            (len(runs) * factor(key[0]), key, runs)
            for key, runs in groups.items()
        ]
        clusters.sort(key=lambda item: item[0], reverse=True)

        report = AttributionReport(
            clusters=[
                AttributionCluster(
                    failure_mode=key[0],
                    defect_class=defect_of[key],
                    cluster_size=len(runs),
                    sample_run_ids=[r.run_id for r in runs],
                    evidence=evidence_of[key],
                )
                for _, key, runs in clusters
            ]
        )
        logger.debug(
            "analyze.exit",
            clusters=len(report.clusters),
            top_mode=report.clusters[0].failure_mode if report.clusters else None,
        )
        return report

    # -- classification ---------------------------------------------------------

    @staticmethod
    def _classify(
        feats: _RunFeatures, sut_config: dict[str, str]
    ) -> tuple[str, tuple[str, ...], str, list[str]]:
        """Return (failure_mode, signature, defect_class, evidence_refs)."""
        skipped_unexecuted = [
            ev
            for ev in feats.skipped
            if not any(ev in cmd for cmd in feats.commands)
        ]
        if skipped_unexecuted:
            evidence_refs: list[str] = list(skipped_unexecuted)
            guided = [
                (ev, path)
                for ev in skipped_unexecuted
                for path, content in sut_config.items()
                if ev in content
            ]
            if guided:
                # A类: 指引已有未执行 — cite the SUT source containing the guidance
                defect = "A"
                for ev, path in guided:
                    ref = f"suts-ref:{path}:{ev}"
                    if ref not in evidence_refs:
                        evidence_refs.append(ref)
            else:
                defect = "B"  # 指引缺失 -> optimization direction
            return "missed_critical_command", tuple(sorted(skipped_unexecuted)), defect, evidence_refs

        if feats.errors:
            return (
                "hallucinated_command",
                tuple(sorted(feats.errors)),
                "B",
                [f"safety:{e}" for e in feats.errors],
            )

        if feats.root_cause_accuracy < 1.0:
            return (
                "causal_misattribution",
                ("root_cause",),
                "B",
                ["root_cause_accuracy<1.0"],
            )

        return "other", ("other",), "B", []
