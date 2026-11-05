"""Unit tests for rsi.instrumentor (DD-06 P0 config-level pipeline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi.core.models import TaskResult
from rsi.instrumentor import (
    CoverageReport,
    Instrumentor,
    InstrumentPlan,
    PlanItem,
    Scanner,
    Planner,
    Injector,
    Verifier,
    ReportGenerator,
    ScanReport,
    VerifyResult,
)
from rsi.instrumentor.report import CoveredItem, NotInstrumentable


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills" / "bgp-check"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    mcp = {
        "mcpServers": {
            "netmiko": {"command": "python", "args": ["-m", "netmiko_srv"]},
        }
    }
    (tmp_path / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "diag.py").write_text("print('x')\n", encoding="utf-8")
    (scripts / "run.sh").write_text("echo hi\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- Scanner


class TestScanner:
    def test_finds_all(self, config_dir: Path):
        report = Scanner.scan(config_dir, "opencode")
        assert [s["name"] for s in report.skills] == ["bgp-check"]
        assert len(report.mcp_servers) == 1
        assert report.mcp_servers[0]["name"] == "netmiko"
        assert report.mcp_servers[0]["command"] == "python"
        assert {Path(s["path"]).name for s in report.scripts} == {"diag.py", "run.sh"}
        assert report.carrier_hooks_supported is True

    def test_carrier_hooks_unsupported(self, config_dir: Path):
        report = Scanner.scan(config_dir, "claude_code")
        assert report.carrier_hooks_supported is False

    def test_empty_dir(self, tmp_path: Path):
        report = Scanner.scan(tmp_path, "opencode")
        assert report.skills == []
        assert report.mcp_servers == []
        assert report.scripts == []
        assert report.knowledge is None


# ---------------------------------------------------------------- Planner


class TestPlanner:
    def test_plan(self, config_dir: Path):
        scan = Scanner.scan(config_dir, "opencode")
        plan = Planner.plan(scan, policy={})
        methods = [(i.target, i.method) for i in plan.items]
        assert ("tool", "mcp_wrap") in methods
        assert ("tool", "hook") in methods
        assert ("skill", "source") in methods
        assert ("script", "source") in methods
        not_source = [i for i in plan.items if i.method != "source"]
        assert all(i.rollback == "overlay" for i in not_source)
        source_items = [i for i in plan.items if i.method == "source"]
        assert all(i.snippet_spec is None for i in source_items)

    def test_no_hooks_when_unsupported(self, config_dir: Path):
        scan = Scanner.scan(config_dir, "claude_code")
        plan = Planner.plan(scan, policy={})
        assert not any(i.method == "hook" for i in plan.items)
        # still wraps MCP
        assert any(i.method == "mcp_wrap" for i in plan.items)


# ---------------------------------------------------------------- Injector


class TestInjector:
    def test_apply(self, config_dir: Path):
        scan = Scanner.scan(config_dir, "opencode")
        plan = Planner.plan(scan, policy={})
        from rsi.carrier.overlay import OverlayBuilder

        overlay = OverlayBuilder.build(config_dir)
        Injector.apply(overlay, plan)

        data = json.loads((overlay / "mcp.json").read_text(encoding="utf-8"))
        srv = data["mcpServers"]["netmiko"]
        assert srv["command"] == "pg-mcp-proxy"
        assert srv["args"][:1] == ["--"]
        assert "python" in srv["args"]
        assert srv["env"]["PG_PROXY_SINK"]
        hook_files = list((overlay / "plugins").glob("*.ts"))
        assert hook_files
        # source markers
        markers = (overlay / "skills" / "bgp-check" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "PG-TRACE" in markers

    def test_original_untouched(self, config_dir: Path):
        original = (config_dir / "mcp.json").read_text(encoding="utf-8")
        scan = Scanner.scan(config_dir, "opencode")
        plan = Planner.plan(scan, policy={})
        from rsi.carrier.overlay import OverlayBuilder

        overlay = OverlayBuilder.build(config_dir)
        Injector.apply(overlay, plan)
        assert (config_dir / "mcp.json").read_text(encoding="utf-8") == original
        assert not (config_dir / "plugins").exists()

    def test_overlay_json_present(self, config_dir: Path):
        from rsi.carrier.overlay import OverlayBuilder

        overlay = OverlayBuilder.build(config_dir)
        assert (overlay / "overlay.json").exists()


# ---------------------------------------------------------------- Verifier


def _ok() -> TaskResult:
    return TaskResult(task_complete=True, root_cause="bgp down")


class TestVerifier:
    def test_pass(self):
        events = [{"event_type": "tool_call"}]
        r = Verifier.verify(_ok(), _ok(), events)
        assert r.passed and r.task_result_match and r.events_produced

    def test_root_cause_mismatch(self):
        other = TaskResult(task_complete=True, root_cause="ospf down")
        r = Verifier.verify(_ok(), other, [{"event_type": "tool_call"}])
        assert not r.passed and not r.task_result_match

    def test_task_complete_mismatch(self):
        other = TaskResult(task_complete=False, root_cause="bgp down")
        assert not Verifier.verify(_ok(), other, [{"event_type": "tool_call"}]).passed

    def test_no_events(self):
        r = Verifier.verify(_ok(), _ok(), [])
        assert not r.passed and not r.events_produced


# ---------------------------------------------------------------- Report


class TestReport:
    def test_generate(self, config_dir: Path):
        scan = Scanner.scan(config_dir, "opencode")
        plan = Planner.plan(scan, policy={})
        verify = Verifier.verify(_ok(), _ok(), [{"event_type": "tool_call"}])
        report = ReportGenerator.generate(plan, scan, verify)
        assert any(c.aspect == "tool" for c in report.covered)
        assert any(n.aspect == "skill" for n in report.not_instrumentable)
        assert any(n.aspect == "script" for n in report.not_instrumentable)
        for c in report.covered:
            assert isinstance(c, CoveredItem) and c.files


# ---------------------------------------------------------------- Facade


class TestInstrumentorFacade:
    @staticmethod
    def _smoke(config_dir: Path) -> TaskResult:
        # B-12: emit a real tool event from the instrumented side so the
        # events-produced criterion is satisfied by genuine tool traffic.
        if (config_dir / "overlay.json").exists():
            sink = config_dir / "trace" / "events.ndjson"
            sink.parent.mkdir(parents=True, exist_ok=True)
            sink.write_text(
                '{"event_type": "tool_call", "data": {"tool": "netmiko"}}\n',
                encoding="utf-8",
            )
        return _ok()

    def test_pipeline(self, config_dir: Path):
        overlay_dir, report = Instrumentor.instrument(
            config_dir, "opencode", self._smoke
        )
        assert overlay_dir != config_dir
        assert overlay_dir.name.startswith("overlay-")
        assert (overlay_dir / "mcp.json").exists()
        assert isinstance(report, CoverageReport)
        assert report.covered

    def test_cleanup_on_verify_failure(self, config_dir: Path):
        events: list = []

        def smoke(cfg: Path) -> TaskResult:
            return _ok()

        # force failure: monkeypatch Verifier via a broken smoke that differs
        calls = {"n": 0}

        def flaky(cfg: Path) -> TaskResult:
            calls["n"] += 1
            if calls["n"] > 1:
                return TaskResult(task_complete=False, root_cause="broke")
            return _ok()

        before = set(config_dir.parent.glob("overlay-*"))
        overlay_dir, report = Instrumentor.instrument(config_dir, "opencode", flaky)
        assert overlay_dir == config_dir
        # overlay cleaned up: no new overlay-* siblings left
        after = set(config_dir.parent.glob("overlay-*"))
        assert after == before


def test_model_shapes():
    plan = InstrumentPlan(items=[PlanItem(target="tool", method="mcp_wrap",
                                          file="mcp.json", snippet_spec=None,
                                          rollback="overlay")])
    assert plan.items[0].target == "tool"
    vr = VerifyResult(passed=True, task_result_match=True, events_produced=True,
                      detail="ok")
    assert vr.passed
