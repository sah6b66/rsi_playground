"""Unit tests: FakeSandboxBackend + SandboxService + LabScheduler (DD-04 §4-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi.core.errors import DeviceError, EnvError
from rsi.core.models import (
    CliEntry,
    DeviceSpec,
    EnvSpec,
    LinkSpec,
    PrepareAction,
    SshAccess,
    Topology,
)
from rsi.sandbox.backends.base import BackendStatus
from rsi.sandbox.backends.fake import FakeDevice, FakeSandboxBackend
from rsi.sandbox.scheduler import LabScheduler, LabSelection
from rsi.sandbox.service import SandboxService

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _env(name: str = "lab-a") -> EnvSpec:
    return EnvSpec(
        name=name,
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
    )


@pytest.fixture
def service(tmp_path: Path) -> SandboxService:
    return SandboxService(
        backends={"fake-1": FakeSandboxBackend()},
        labs_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# FakeDevice rule table
# ---------------------------------------------------------------------------


class TestFakeDeviceExec:
    def test_config_command_appends_to_running(self):
        d = FakeDevice()
        out = d.exec("interface GE0/0/1")
        assert out == ""
        out = d.exec("shutdown")
        assert out == ""
        assert "interface GE0/0/1" in d.running_config
        assert "shutdown" in d.running_config
        assert "interface GE0/0/1" in d.config_lines

    def test_display_current_configuration(self):
        d = FakeDevice()
        d.exec("sysname RT-A")
        d.exec("interface GE0/0/1")
        out = d.exec("display current-configuration")
        assert "sysname RT-A" in out
        assert "interface GE0/0/1" in out

    def test_show_running_config_alias(self):
        d = FakeDevice()
        d.exec("bgp 65001")
        assert "bgp 65001" in d.exec("show running-config")

    def test_display_clock_has_timestamp(self):
        out = FakeDevice().exec("display clock")
        assert "2026" in out
        assert "Error" not in out

    def test_known_display_canned(self):
        d = FakeDevice()
        d.exec("interface GE0/0/1")
        out = d.exec("display interface brief")
        assert "GE0/0/1" in out

    def test_unknown_display_errors(self):
        out = FakeDevice().exec("display frobnicate")
        assert out.startswith("Error:")

    def test_view_commands_not_recorded(self):
        d = FakeDevice()
        d.exec("system-view")
        d.exec("return")
        assert d.running_config == []


class TestFakeSandboxBackend:
    @pytest.mark.asyncio
    async def test_build_and_wait_ready(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        assert set(lab.endpoints) == {"R1", "R2"}
        assert b.active_lab_count == 1
        report = await b.wait_ready(lab)
        assert report.passed is True
        assert len(report.checks) == 2

    @pytest.mark.asyncio
    async def test_snapshot_restore(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        b.exec(lab, "R1", "sysname CHANGED")
        snap = await b.snapshot(lab, "mid")
        assert snap.impl_kind == "degraded"
        b.exec(lab, "R1", "sysname AGAIN")
        await b.restore(lab, snap)
        cfg = b.exec(lab, "R1", "display current-configuration")
        assert "sysname CHANGED" in cfg
        assert "sysname AGAIN" not in cfg

    @pytest.mark.asyncio
    async def test_reset_restores_build_state(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        b.exec(lab, "R1", "sysname X")
        await b.reset(lab)
        assert "sysname X" not in b.exec(lab, "R1", "display current-configuration")

    @pytest.mark.asyncio
    async def test_destroy(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        await b.destroy(lab)
        assert b.active_lab_count == 0
        with pytest.raises(EnvError):
            b.exec(lab, "R1", "display clock")

    @pytest.mark.asyncio
    async def test_unknown_device_exec(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        with pytest.raises(EnvError):
            b.exec(lab, "NOPE", "display clock")
        await b.destroy(lab)

    @pytest.mark.asyncio
    async def test_apply_fault_is_noop_stub(self):
        b = FakeSandboxBackend()
        lab = await b.build(_env())
        before = b.exec(lab, "R1", "display current-configuration")
        await b.apply_fault(lab, {"kind": "link_down"})  # type: ignore[arg-type]
        assert b.exec(lab, "R1", "display current-configuration") == before


# ---------------------------------------------------------------------------
# LabScheduler
# ---------------------------------------------------------------------------


class TestLabScheduler:
    def test_explicit_key_wins(self):
        b1, b2 = FakeSandboxBackend(), FakeSandboxBackend()
        s = LabScheduler({"fake-2": b2, "fake-1": b1})
        sel = s.select(_env(), explicit_key="fake-2")
        assert sel.backend_key == "fake-2"
        assert "explicit" in sel.reason.lower()

    def test_unknown_explicit_key(self):
        s = LabScheduler({"fake-1": FakeSandboxBackend()})
        with pytest.raises(EnvError):
            s.select(_env(), explicit_key="nope")

    def test_least_active_same_type(self):
        b1, b2 = FakeSandboxBackend(), FakeSandboxBackend()
        s = LabScheduler({"fake-1": b1, "fake-2": b2})

        async def go():
            await b1.build(_env("busy"))

        import asyncio

        asyncio.run(go())
        sel = s.select(_env())
        assert sel.backend_key == "fake-2"

    def test_no_matching_type(self):
        s = LabScheduler({"fake-1": FakeSandboxBackend()})
        env = _env().model_copy(update={"backend": "pnetlab"})
        with pytest.raises(EnvError):
            s.select(env)

    def test_selection_model(self):
        sel = LabSelection(backend_key="fake-1", reason="r")
        assert sel.backend_key == "fake-1"


# ---------------------------------------------------------------------------
# SandboxService: build/prepare/exec/restore cycle
# ---------------------------------------------------------------------------


class TestServiceCycle:
    @pytest.mark.asyncio
    async def test_exec_cli_cycle(self, service: SandboxService):
        lab = await service.build_lab(_env())
        actions = [
            PrepareAction(
                action="exec_cli",
                entries=[
                    CliEntry(device="R1", cli_lines=["sysname BEFORE", "interface GE0/0/1"]),
                    CliEntry(device="R2", cli_lines=["sysname R2OLD"]),
                ],
            )
        ]
        applied = await service.prepare(lab, "fault/link-01", actions)
        assert len(applied) == 1
        assert set(applied[0].backup_files) == {"R1", "R2"}
        for path in applied[0].backup_files.values():
            assert Path(path).is_file()

        # config applied
        assert "sysname BEFORE" in service.exec_readonly(
            lab, "R1", "display current-configuration"
        )

        # mutate after prepare (simulating task execution)
        service._backends["fake-1"].exec(lab, "R1", "sysname BROKEN")

        await service.restore_task(lab, applied)
        cfg = service.exec_readonly(lab, "R1", "display current-configuration")
        # backup was captured BEFORE prepare -> device back to pre-task state
        assert "sysname BEFORE" not in cfg
        assert "sysname BROKEN" not in cfg

    @pytest.mark.asyncio
    async def test_exec_readonly_whitelist(self, service: SandboxService):
        lab = await service.build_lab(_env())
        assert "2026" in service.exec_readonly(lab, "R1", "display clock")
        with pytest.raises(DeviceError):
            service.exec_readonly(lab, "R1", "system-view")
        with pytest.raises(DeviceError):
            service.exec_readonly(lab, "R1", "reboot")
        with pytest.raises(DeviceError):
            service.exec_readonly(lab, "NOPE", "display clock")

    @pytest.mark.asyncio
    async def test_l2_fallback(self, service: SandboxService):
        lab = await service.build_lab(_env())
        applied = await service.prepare(
            lab,
            "fault/link-01",
            [PrepareAction(
                action="exec_cli",
                entries=[CliEntry(device="R1", cli_lines=["sysname KEEPME"])],
            )],
        )
        # destroy backups -> L1 recovery impossible -> EnvError (triggers L2)
        for path in applied[0].backup_files.values():
            Path(path).unlink()
        with pytest.raises(EnvError):
            await service.restore_task(lab, applied)
        # L2: reset + restore snap0
        await service.reset(lab)
        snap0 = service.snapshots(lab.lab_id)[0]
        await service.restore(lab, snap0)
        cfg = service.exec_readonly(lab, "R1", "display current-configuration")
        assert "sysname KEEPME" not in cfg  # back to build-time state

    @pytest.mark.asyncio
    async def test_lab_isolation(self, tmp_path: Path):
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()}, labs_dir=tmp_path
        )
        lab1 = await service.build_lab(_env("env-one"))
        lab2 = await service.build_lab(_env("env-two"))
        assert lab1.lab_id != lab2.lab_id
        backend = service._backends["fake-1"]
        backend.exec(lab1, "R1", "sysname LAB1ONLY")
        cfg2 = backend.devices(lab2)["R1"].exec("display current-configuration")
        assert "LAB1ONLY" not in cfg2
        cfg1 = backend.devices(lab1)["R1"].exec("display current-configuration")
        assert "LAB1ONLY" in cfg1

    @pytest.mark.asyncio
    async def test_health_check(self, service: SandboxService):
        lab = await service.build_lab(_env())
        assert await service.health_check(lab) is True

    @pytest.mark.asyncio
    async def test_ops_journal(self, service: SandboxService):
        env = _env()
        lab = await service.build_lab(env)
        await service.prepare(
            lab,
            "fault/link-01",
            [PrepareAction(
                action="exec_cli",
                entries=[CliEntry(device="R1", cli_lines=["sysname J1"])],
            )],
        )
        journal = Path(service.labs_dir) / lab.lab_id / "ops.jsonl"
        assert journal.is_file()
        lines = [json.loads(x) for x in journal.read_text().splitlines() if x.strip()]
        assert lines[0]["record"] == "lab_build"
        assert lines[0]["env"] == env.name
        assert any(l["record"] == "applied" and l["applied"]["entries"] for l in lines)

    @pytest.mark.asyncio
    async def test_backend_status(self, service: SandboxService):
        statuses = await service.backend_status()
        assert len(statuses) == 1
        s = statuses[0]
        assert isinstance(s, BackendStatus)
        assert s.key == "fake-1"
        assert s.kind == "fake"
        assert s.active_labs == 0

    @pytest.mark.asyncio
    async def test_destroy(self, service: SandboxService):
        lab = await service.build_lab(_env())
        await service.destroy(lab)
        with pytest.raises(DeviceError):
            service.exec_readonly(lab, "R1", "display clock")

    @pytest.mark.asyncio
    async def test_prepare_failure_raises(self, service: SandboxService):
        lab = await service.build_lab(_env())
        backend = service._backends["fake-1"]
        # force a device-level failure by making R1 gone mid-flight
        backend.remove_device(lab.lab_id, "R1")
        with pytest.raises(EnvError):
            await service.prepare(
                lab,
                "fault/link-01",
                [PrepareAction(
                    action="exec_cli",
                    entries=[CliEntry(device="R1", cli_lines=["sysname X"])],
                )],
            )
        await backend.destroy(lab)


# ---------------------------------------------------------------------------
# BackendStatus model
# ---------------------------------------------------------------------------


def test_backend_status_model():
    st = BackendStatus(key="k", kind="fake", active_labs=0, available=True, detail="ok")
    assert st.key == "k"
