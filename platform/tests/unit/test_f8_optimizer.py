"""F8 tests: D-10 SftAssessor report/settings + D-12 editable_elements."""

from __future__ import annotations

from pathlib import Path

from rsi.optimizer.sft import SftAssessor
from rsi.optimizer.targets import TargetRegistry


def _clusters() -> list[dict]:
    return [
        {
            "failure_mode": "missed_critical_command",
            "defect_class": "B",
            "cluster_size": 30,
            "sample_run_ids": ["r1"],
            "evidence": [],
        }
    ]


def _history(rounds: int) -> list[dict]:
    return [
        {
            "failure_mode": "missed_critical_command",
            "decision": "reverted",
            "proposal_type": "add_knowledge",
        }
        for _ in range(rounds)
    ]


class TestSftSettingsAndReport:
    def test_thresholds_injected_from_settings(self):
        # 4 rounds of no-gain: default (5) rejects, lowered (4) accepts
        assert SftAssessor().assess(_clusters(), _history(4), 60) is None
        strict = SftAssessor(no_gain_rounds=4, min_samples=50)
        assert strict.assess(_clusters(), _history(4), 60) is not None
        # min_samples injected too
        assert (
            SftAssessor(no_gain_rounds=5, min_samples=100).assess(
                _clusters(), _history(6), 60
            )
            is None
        )
        assert (
            SftAssessor(no_gain_rounds=5, min_samples=60).assess(
                _clusters(), _history(6), 60
            )
            is not None
        )

    def test_report_file_written(self, tmp_path):
        assessor = SftAssessor(artifacts_dir=tmp_path)
        report = assessor.assess(
            _clusters(), _history(6), 60, exp_id="exp-123", batch_id="batch-9"
        )
        assert report is not None
        path = Path(report["report_path"])
        assert path.exists()
        assert path == tmp_path / "batch-9" / "reports" / "sft-exp-123.md"
        text = path.read_text(encoding="utf-8")
        assert "SFT" in text
        assert "准入条件" in text
        assert "missed_critical_command" in text

    def test_report_owner_defaults_to_exp_id(self, tmp_path):
        assessor = SftAssessor(artifacts_dir=tmp_path)
        report = assessor.assess(_clusters(), _history(6), 60, exp_id="exp-7")
        assert Path(report["report_path"]) == tmp_path / "exp-7" / "reports" / "sft-exp-7.md"

    def test_no_report_written_without_exp_id(self):
        report = SftAssessor().assess(_clusters(), _history(6), 60)
        assert report is not None
        assert "report_path" not in report


class TestEditableElements:
    def test_eight_elements_with_glob_and_examples(self):
        elements = TargetRegistry().editable_elements()
        assert len(elements) == 8
        targets = {e["target"] for e in elements}
        assert targets == {
            "system_prompt", "skills", "scripts", "mcp_config",
            "mcp_src", "memory", "knowledge", "model_config",
        }
        registry = TargetRegistry()
        for e in elements:
            assert e["path_glob"] and e["description"] and e["examples"]
            # the glob mirrors the whitelist regex: the example path matches
            example = e["examples"][0].replace("{sut}", "sut-x")
            assert registry.match_target(example) == e["target"]

    def test_enabled_filter(self):
        elements = TargetRegistry().editable_elements(enabled={"knowledge"})
        assert len(elements) == 1
        assert elements[0]["target"] == "knowledge"
