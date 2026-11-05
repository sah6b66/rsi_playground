"""SftAssessor — DD-11 §5.3 SFT/DPO need assessment.

SFT/DPO is a *suggestion-only* escalation: the assessor never executes
training, it returns a report when all three admission criteria hold:

1. the same defect class has produced no gain for >= 5 optimization rounds;
2. at least 50 failed samples exist to build a training set;
3. the cheaper knowledge/skill levers have already been tried (and failed).
"""

from __future__ import annotations

from typing import Any

from rsi.log import get_logger

__all__ = ["SftAssessor"]

logger = get_logger(__name__)

NO_GAIN_THRESHOLD = 5
FAILED_SAMPLE_THRESHOLD = 50

_FAILED_DECISIONS = frozenset({"reverted", "advise_manual", "rejected", "revert"})
_KNOWLEDGE_LEVERS = frozenset(
    {"add_knowledge", "add_runbook", "add_fewshot_example", "modify_prompt"}
)
_KNOWLEDGE_TARGETS = frozenset({"knowledge", "skills"})


def _field(entry: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in entry and entry[name] is not None:
            return entry[name]
    return None


class SftAssessor:
    """Assess whether SFT/DPO should be *suggested* (never executed)."""

    def assess(
        self,
        defect_b_clusters: list[dict[str, Any]],
        opt_history: list[dict[str, Any]],
        failed_sample_count: int,
    ) -> dict[str, Any] | None:
        """Return a suggestion report when all 3 admission criteria hold."""
        # Criterion 1: same defect class (failure mode) with no-gain rounds >= 5.
        no_gain_by_mode: dict[str, int] = {}
        for entry in opt_history:
            mode = _field(entry, "failure_mode")
            decision = str(_field(entry, "decision", "action") or "").lower()
            if mode is None:
                continue
            if decision in _FAILED_DECISIONS:
                no_gain_by_mode[str(mode)] = no_gain_by_mode.get(str(mode), 0) + 1

        cluster_modes = [
            str(_field(c, "failure_mode") or "") for c in defect_b_clusters
        ]
        qualified_modes = [
            m
            for m in cluster_modes
            if no_gain_by_mode.get(m, 0) >= NO_GAIN_THRESHOLD
        ]
        criterion_1 = bool(qualified_modes)

        # Criterion 2: enough failed samples to train on.
        criterion_2 = failed_sample_count >= FAILED_SAMPLE_THRESHOLD

        # Criterion 3: knowledge / skill levers already attempted.
        levers_tried: set[str] = set()
        for entry in opt_history:
            proposal = _field(entry, "proposal_type", "lever")
            target = _field(entry, "target")
            if proposal is not None and str(proposal) in _KNOWLEDGE_LEVERS:
                levers_tried.add(str(proposal))
            if target is not None and str(target) in _KNOWLEDGE_TARGETS:
                levers_tried.add(str(target))
        criterion_3 = bool(levers_tried)

        logger.debug(
            "assess.exit",
            criterion_1=criterion_1,
            criterion_2=criterion_2,
            criterion_3=criterion_3,
            failed_samples=failed_sample_count,
        )
        if not (criterion_1 and criterion_2 and criterion_3):
            return None

        return {
            "recommendation": "SFT/DPO",
            "only_suggest": True,  # never execute autonomously (DD-11 §5.3)
            "defect_class": "B",
            "failure_modes": sorted(set(qualified_modes)),
            "no_gain_rounds": {
                m: no_gain_by_mode.get(m, 0) for m in sorted(set(qualified_modes))
            },
            "failed_sample_count": failed_sample_count,
            "levers_tried": sorted(levers_tried),
            "criteria": {
                "same_defect_no_gain_rounds": criterion_1,
                "failed_samples_ge_50": criterion_2,
                "knowledge_skill_tried": criterion_3,
            },
        }
