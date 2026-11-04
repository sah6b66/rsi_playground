"""Unit tests: OverlayBuilder."""

from __future__ import annotations

import json
from pathlib import Path

from rsi.carrier.overlay import OverlayBuilder


def _make_config(tmp_path: Path) -> Path:
    config = tmp_path / "sut" / "config"
    (config / "skills").mkdir(parents=True)
    (config / "CLAUDE.md").write_text("base", encoding="utf-8")
    (config / "skills" / "bgp.md").write_text("skill", encoding="utf-8")
    return config


class TestBuild:
    def test_copies_files(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        overlay = OverlayBuilder.build(config)
        try:
            assert overlay.parent == config.parent
            assert overlay.name.startswith("overlay-")
            assert (overlay / "CLAUDE.md").read_text(encoding="utf-8") == "base"
            assert (overlay / "skills" / "bgp.md").read_text(encoding="utf-8") == "skill"
        finally:
            OverlayBuilder.cleanup(overlay)

    def test_overlay_json_written(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        overlay = OverlayBuilder.build(config, plan=None)
        try:
            meta = json.loads((overlay / "overlay.json").read_text(encoding="utf-8"))
            assert meta["source"] == str(config)
            assert meta["destroyable"] is True
            assert meta["plan_summary"] is None
        finally:
            OverlayBuilder.cleanup(overlay)

    def test_source_untouched(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        before = sorted(str(p.relative_to(config)) for p in config.rglob("*"))
        overlay = OverlayBuilder.build(config)
        try:
            after = sorted(str(p.relative_to(config)) for p in config.rglob("*"))
            assert before == after
            assert not (config / "overlay.json").exists()
        finally:
            OverlayBuilder.cleanup(overlay)


class TestCleanup:
    def test_removes_overlay(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        overlay = OverlayBuilder.build(config)
        assert overlay.exists()
        OverlayBuilder.cleanup(overlay)
        assert not overlay.exists()
        assert config.exists()  # source survives

    def test_missing_dir_noop(self, tmp_path: Path) -> None:
        OverlayBuilder.cleanup(tmp_path / "overlay-nonexistent")

    def test_non_destroyable_dir_survives(self, tmp_path: Path) -> None:
        guard = tmp_path / "keepme"
        guard.mkdir()
        OverlayBuilder.cleanup(guard)  # no overlay.json -> not destroyable
        assert guard.exists()
