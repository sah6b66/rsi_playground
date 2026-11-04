"""FakeSandboxBackend (DD-04 §5.6) — in-memory virtual devices.

Offline test base for BatchRunner, judge and every downstream module: each
lab gets a dict of :class:`FakeDevice` whose ``exec`` follows a deterministic
rule table — ``display current-configuration`` replays the configured
command sequence, other read-only commands get canned (config-derived where
useful) output, config commands append to the running config and return
empty (success). Snapshots are deep copies tagged ``degraded``; restore =
replace current state with the snapshot copy.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from rsi.core.errors import EnvError
from rsi.core.models import EnvSpec, LabHandle, SnapRef
from rsi.core.naming import new_id
from rsi.sandbox.backends.base import SandboxBackend
from rsi.sandbox.readiness import CheckResult, ReadyReport

__all__ = ["FakeDevice", "FakeSandboxBackend"]

# commands that manipulate CLI views / persistence but are not configuration
_VIEW_COMMANDS = frozenset(
    {"system-view", "return", "quit", "end", "configure terminal", "save", "y"}
)

_READONLY_PREFIXES = ("display", "show", "ping", "tracert", "screen-length", "disp")


class FakeDevice:
    """One in-memory device: config state + deterministic exec rule table."""

    def __init__(self, name: str = "dev", vendor: str = "vrp") -> None:
        self.name = name
        self.vendor = vendor
        self.config_lines: set[str] = set()  # deduplicated configured commands
        self.running_config: list[str] = []  # ordered configured commands

    # -- rule table ----------------------------------------------------------

    def exec(self, cli: str) -> str:
        cmd = cli.strip()
        lowered = cmd.lower()
        if lowered.startswith(("display current-configuration", "show running-config")):
            return self._current_configuration()
        if lowered.startswith(_READONLY_PREFIXES):
            return self._display(cmd)
        if lowered in _VIEW_COMMANDS:
            return ""
        # any other command = configuration command (idempotent SET semantics)
        self.running_config.append(cmd)
        self.config_lines.add(cmd)
        return ""

    def _current_configuration(self) -> str:
        if not self.running_config:
            return "#\nreturn"
        return "\n".join(self.running_config)

    def _display(self, cmd: str) -> str:
        lowered = cmd.lower()
        if lowered.startswith(("display clock", "show clock")):
            now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            return f"{now}\nUTC\n"
        if lowered.startswith(("display version", "show version")):
            return (
                "Huawei Versatile Routing Platform Software\n"
                "VRP (R) software, Version 8.180 (fake)\n"
                "Huawei fake device uptime is 0 days, 0 hours\n"
            )
        if lowered.startswith(("display interface brief", "show interface brief")):
            ports = [
                m.group(1)
                for m in (
                    re.match(r"(?:interface|port)\s+(\S+)$", line)
                    for line in self.running_config
                )
                if m
            ]
            rows = ["Interface  PHY   Protocol  InUti  OutUti"]
            rows += [f"{p:<10}  up    up        0%     0%" for p in ports]
            return "\n".join(rows) + "\n"
        if lowered.startswith("display this"):
            return self._current_configuration()
        return "Error: Unrecognized command at '^' position."

    # -- state ---------------------------------------------------------------

    def snapshot(self) -> FakeDevice:
        clone = FakeDevice(self.name, self.vendor)
        clone.config_lines = set(self.config_lines)
        clone.running_config = list(self.running_config)
        return clone

    def load_from(self, other: FakeDevice) -> None:
        self.config_lines = set(other.config_lines)
        self.running_config = list(other.running_config)


class _LabState:
    def __init__(self, lab_id: str, devices: dict[str, FakeDevice]) -> None:
        self.lab_id = lab_id
        self.devices = devices
        self.initial = {n: d.snapshot() for n, d in devices.items()}


class FakeSandboxBackend(SandboxBackend):
    kind = "fake"

    def __init__(self) -> None:
        self._labs: dict[str, _LabState] = {}
        self._snaps: dict[str, dict[str, FakeDevice]] = {}

    # -- lifecycle -----------------------------------------------------------

    async def build(self, env: EnvSpec) -> LabHandle:
        lab_id = new_id("lab")
        devices = {
            d.name: FakeDevice(d.name, d.type) for d in env.devices
        }
        self._labs[lab_id] = _LabState(lab_id, devices)
        return LabHandle(
            lab_id=lab_id,
            host="fake",
            backend=self.kind,
            endpoints=dict(env.ssh_access),
        )

    async def wait_ready(self, lab: LabHandle, timeout_s: int = 600) -> ReadyReport:
        state = self._require_lab(lab)
        checks = [
            CheckResult(type="ssh_reachable", device=n, passed=True, detail="in-memory")
            for n in sorted(state.devices)
        ]
        return ReadyReport(passed=True, checks=checks, elapsed_s=0.0)

    async def snapshot(self, lab: LabHandle, name: str) -> SnapRef:
        state = self._require_lab(lab)
        snap_id = new_id("snap")
        self._snaps[snap_id] = {
            n: d.snapshot() for n, d in state.devices.items()
        }
        return SnapRef(snapshot_id=snap_id, impl_kind="degraded", backend_ref=name)

    async def restore(self, lab: LabHandle, snap: SnapRef) -> None:
        state = self._require_lab(lab)
        try:
            saved = self._snaps[snap.snapshot_id]
        except KeyError:
            raise EnvError(
                f"unknown snapshot {snap.snapshot_id!r}",
                context={"snapshot_id": snap.snapshot_id, "lab_id": lab.lab_id},
            ) from None
        for name, device in state.devices.items():
            if name in saved:
                device.load_from(saved[name])

    async def reset(self, lab: LabHandle) -> None:
        state = self._require_lab(lab)
        for name, device in state.devices.items():
            device.load_from(state.initial[name])

    async def destroy(self, lab: LabHandle) -> None:
        self._labs.pop(lab.lab_id, None)

    # -- exec ------------------------------------------------------------------

    def exec(self, lab: LabHandle, device: str, cli: str) -> str:
        state = self._require_lab(lab)
        try:
            dev = state.devices[device]
        except KeyError:
            raise EnvError(
                f"device {device!r} not present in fake lab {lab.lab_id!r}",
                context={"device": device, "lab_id": lab.lab_id},
            ) from None
        return dev.exec(cli)

    # -- introspection (tests / scheduler) -----------------------------------

    @property
    def active_lab_count(self) -> int:
        return len(self._labs)

    def devices(self, lab: LabHandle) -> dict[str, FakeDevice]:
        return self._require_lab(lab).devices

    def remove_device(self, lab_id: str, device: str) -> None:
        state = self._labs.get(lab_id)
        if state is not None:
            state.devices.pop(device, None)

    # -- internals -------------------------------------------------------------

    def _require_lab(self, lab: LabHandle) -> _LabState:
        return self._require_lab_id(lab.lab_id)

    def _require_lab_id(self, lab_id: str) -> _LabState:
        try:
            return self._labs[lab_id]
        except KeyError:
            raise EnvError(
                f"lab {lab_id!r} not present in fake backend",
                context={"lab_id": lab_id},
            ) from None
