"""Unit tests: TargetRegistry path domain validation (DD-11 §3, TU-013)."""

from __future__ import annotations

import pytest

from rsi.core.errors import TargetViolationError
from rsi.optimizer.targets import FROZEN_PATHS, TARGET_PATHS, TargetRegistry

ALL_TARGETS = set(TARGET_PATHS)


class TestTableShape:
    def test_eight_targets(self):
        assert len(TARGET_PATHS) == 8

    def test_frozen_paths(self):
        assert sorted(FROZEN_PATHS) == ["evalsets/.*", "judge/.*", "prompts/.*"]


class TestValidate:
    @pytest.mark.parametrize(
        "path",
        [
            "suts/sut-1/system-prompt",
            "suts/sut-1/system-prompt.md",
            "suts/sut-1/skills/diag.md",
            "suts/sut-1/scripts/probe.py",
            "suts/sut-1/mcp.json",
            "suts/sut-1/mcp-src/server.py",
            "suts/sut-1/memory/session-3.md",
            "suts/sut-1/knowledge/bgp.md",
            "suts/sut-1/model.json",
        ],
    )
    def test_valid_paths_pass(self, path):
        TargetRegistry().validate(path, ALL_TARGETS)  # no raise

    @pytest.mark.parametrize(
        "path",
        [
            "judge/deterministic.py",
            "prompts/hypothesis/diagnose.v1",
            "evalsets/set-a/tasks.jsonl",
        ],
    )
    def test_frozen_paths_reject(self, path):
        with pytest.raises(TargetViolationError) as ei:
            TargetRegistry().validate(path, ALL_TARGETS)
        assert ei.value.context.get("reason") == "frozen_domain"

    def test_disabled_target_reject(self):
        with pytest.raises(TargetViolationError) as ei:
            TargetRegistry().validate(
                "suts/sut-1/skills/diag.md", enabled_targets={"system_prompt"}
            )
        assert ei.value.context.get("reason") == "target_not_enabled"
        assert ei.value.context.get("target") == "skills"

    def test_unknown_path_reject(self):
        with pytest.raises(TargetViolationError) as ei:
            TargetRegistry().validate("suts/sut-1/other/file.txt", ALL_TARGETS)
        assert ei.value.context.get("reason") == "path_out_of_domain"

    def test_non_suts_path_reject(self):
        with pytest.raises(TargetViolationError):
            TargetRegistry().validate("etc/passwd", ALL_TARGETS)

    def test_prefix_trick_reject(self):
        # "suts/x/evil.md" prefix of a frozen path is still out of frozen domain
        # but a frozen path with suts prefix must not be smuggled through.
        with pytest.raises(TargetViolationError):
            TargetRegistry().validate("suts/sut-1/judge/x.py", ALL_TARGETS)
