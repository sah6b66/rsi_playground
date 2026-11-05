"""TU: DeterministicCritic + ChangeApplier + SftAssessor (DD-11)."""

from __future__ import annotations

import pytest

from rsi.core.errors import TargetViolationError
from rsi.optimizer.applier import ChangeApplier
from rsi.optimizer.critic import DeterministicCritic
from rsi.optimizer.sft import SftAssessor
from rsi.optimizer.targets import TARGET_PATHS

ALL_TARGETS = set(TARGET_PATHS)


def _file(path: str, **overrides) -> dict:
    fc = {"path": path, "op": "edit", "content": "x", "target": None, "declared": True}
    fc.update(overrides)
    return fc


class TestDeterministicCritic:
    def test_valid_changeset_passes(self) -> None:
        critic = DeterministicCritic()
        files = [_file("suts/sut-1/system-prompt.md", target="system_prompt")]
        assert critic.check(files, ALL_TARGETS) == []

    def test_path_out_of_domain(self) -> None:
        failures = DeterministicCritic().check(
            [_file("etc/passwd")], ALL_TARGETS
        )
        assert any(f.startswith("path_out_of_domain") for f in failures)

    def test_frozen_domain(self) -> None:
        failures = DeterministicCritic().check(
            [_file("judge/deterministic.py")], ALL_TARGETS
        )
        assert any(f.startswith("frozen_domain") for f in failures)
        assert not any(f.startswith("path_out_of_domain") for f in failures)

    def test_target_not_enabled(self) -> None:
        failures = DeterministicCritic().check(
            [_file("suts/sut-1/skills/diag.md")], enabled_targets={"system_prompt"}
        )
        assert any(f.startswith("target_not_enabled") for f in failures)

    def test_declaration_mismatch(self) -> None:
        # declares skills but the path belongs to system_prompt
        failures = DeterministicCritic().check(
            [_file("suts/sut-1/system-prompt.md", target="skills")], ALL_TARGETS
        )
        assert any(f.startswith("declaration_mismatch") for f in failures)

    def test_declaration_mismatch_via_flag(self) -> None:
        failures = DeterministicCritic().check(
            [_file("suts/sut-1/skills/diag.md", target="skills", declared=False)],
            ALL_TARGETS,
        )
        assert any(f.startswith("declaration_mismatch") for f in failures)

    def test_multi_target(self) -> None:
        failures = DeterministicCritic().check(
            [
                _file("suts/sut-1/system-prompt.md", target="system_prompt"),
                _file("suts/sut-1/skills/diag.md", target="skills"),
            ],
            ALL_TARGETS,
        )
        assert any(f.startswith("multi_target") for f in failures)

    def test_frozen_check_precedes_domain_check(self) -> None:
        # frozen paths report frozen_domain, never path_out_of_domain
        failures = DeterministicCritic().check(
            [_file("prompts/hypothesis/diagnose.v1")], ALL_TARGETS
        )
        assert failures == ["frozen_domain: prompts/hypothesis/diagnose.v1"]


class TestChangeApplier:
    def test_valid_diff_hypothesis_builds_changeset(self) -> None:
        diff = (
            "diff --git a/suts/sut-1/system-prompt.md b/suts/sut-1/system-prompt.md\n"
            "+ improve root-cause guidance\n"
        )
        version = ChangeApplier().apply(
            "sut-1",
            {"hypothesis_id": "h1", "diff_content": diff},
            ALL_TARGETS,
        )
        assert isinstance(version, str) and len(version) == 40

    def test_valid_model_config_patch(self) -> None:
        version = ChangeApplier().apply(
            "sut-1",
            {"hypothesis_id": "h2", "model_config_patch": {"model": "glm-4.7"}},
            enabled_targets={"model_config"},
        )
        assert len(version) == 40

    def test_model_config_target_disabled_rejected(self) -> None:
        with pytest.raises(TargetViolationError) as ei:
            ChangeApplier().apply(
                "sut-1",
                {"hypothesis_id": "h2", "model_config_patch": {"model": "x"}},
                enabled_targets={"system_prompt"},
            )
        assert ei.value.context.get("reason") == "target_not_enabled"

    def test_frozen_path_rejected(self) -> None:
        diff = "diff --git a/judge/rules b/judge/rules\n+ cheat\n"
        with pytest.raises(TargetViolationError) as ei:
            ChangeApplier().apply(
                "sut-1", {"hypothesis_id": "h3", "diff_content": diff}, ALL_TARGETS
            )
        # judge/rules is anchored under suts/sut-1/ -> out of domain
        assert ei.value.context.get("reason") in ("frozen_domain", "path_out_of_domain")

    def test_out_of_domain_path_rejected(self) -> None:
        diff = "+++ b/suts/sut-1/unknown/target.txt\n+ x\n"
        with pytest.raises(TargetViolationError) as ei:
            ChangeApplier().apply(
                "sut-1", {"hypothesis_id": "h4", "diff_content": diff}, ALL_TARGETS
            )
        assert ei.value.context.get("reason") == "path_out_of_domain"

    def test_empty_hypothesis_rejected(self) -> None:
        with pytest.raises(TargetViolationError):
            ChangeApplier().apply("sut-1", {"hypothesis_id": "h5"}, ALL_TARGETS)


class TestSftAssessor:
    def _clusters(self) -> list[dict]:
        return [
            {"failure_mode": "missed_critical_command", "defect_class": "B",
             "cluster_size": 30, "sample_run_ids": ["r1"], "evidence": []}
        ]

    def _history(self, rounds: int) -> list[dict]:
        # rounds of no-gain for the same failure mode, with knowledge tried
        hist = [
            {"failure_mode": "missed_critical_command", "decision": "reverted",
             "proposal_type": "add_knowledge"}
            for _ in range(rounds)
        ]
        return hist

    def test_all_criteria_met_returns_report(self) -> None:
        report = SftAssessor().assess(self._clusters(), self._history(5), 60)
        assert report is not None
        assert report["recommendation"] == "SFT/DPO"
        assert report["only_suggest"] is True
        assert report["defect_class"] == "B"
        assert report["failed_sample_count"] == 60
        assert all(report["criteria"].values())

    def test_too_few_no_gain_rounds_returns_none(self) -> None:
        assert SftAssessor().assess(self._clusters(), self._history(4), 60) is None

    def test_too_few_failed_samples_returns_none(self) -> None:
        assert SftAssessor().assess(self._clusters(), self._history(6), 49) is None

    def test_knowledge_not_tried_returns_none(self) -> None:
        history = [
            {"failure_mode": "missed_critical_command", "decision": "reverted"}
            for _ in range(6)
        ]
        assert SftAssessor().assess(self._clusters(), history, 100) is None

    def test_no_clusters_returns_none(self) -> None:
        assert SftAssessor().assess([], self._history(6), 100) is None
