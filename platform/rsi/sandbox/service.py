"""SandboxService (DD-04 §4.1) — the seven-operation sandbox facade.

Orchestrates a backend adapter (lab lifecycle + device exec) with the
ConfigApiCaller (call_api prepare/undo), the readiness check and an ops
journal (``labs_dir/<lab_id>/ops.jsonl``) that records every applied action
for audit and L2 recovery replay. Recovery follows ADR-20: L1 per-task
(backup replay / API undo) with <=2 retries, then the caller escalates to
L2 (lab reset + snap0 restore).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rsi.core.errors import DeviceError, EnvError
from rsi.log import get_logger
from rsi.core.models import (
    AppliedAction,
    EnvSpec,
    LabHandle,
    PrepareAction,
    SnapRef,
)
from rsi.sandbox.backends.base import BackendStatus, SandboxBackend
from rsi.sandbox.config_api import ConfigApiCaller
from rsi.sandbox.readiness import ReadyReport
from rsi.sandbox.scheduler import LabScheduler

__all__ = ["READONLY_PREFIXES", "OpsJournal", "SandboxService"]

READONLY_PREFIXES: tuple[str, ...] = (
    "display",
    "show",
    "ping",
    "tracert",
    "screen-length",
    "disp",
)

_RESTORE_MAX_ATTEMPTS = 3  # 1 initial + 2 retries (DD-04 §4.1 restore_task)
_ERROR_OUTPUT_PREFIX = "Error:"

logger = get_logger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class OpsJournal:
    """Append-only JSONL journal at ``labs_dir/<lab_id>/ops.jsonl``."""

    def __init__(self, labs_dir: Path, lab_id: str) -> None:
        self._lab_dir = labs_dir / lab_id
        self._lab_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._lab_dir / "ops.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SandboxService:
    def __init__(
        self,
        backends: dict[str, SandboxBackend],
        labs_dir: Path | str,
        config_api: ConfigApiCaller | None = None,
        scheduler: LabScheduler | None = None,
    ) -> None:
        self._backends = backends
        self.labs_dir = Path(labs_dir)
        self._config_api = config_api or ConfigApiCaller()
        self._scheduler = scheduler or LabScheduler(backends)
        self._lab_backend: dict[str, str] = {}
        self._envs: dict[str, EnvSpec] = {}
        self._snapshots: dict[str, list[SnapRef]] = {}
        self._journals: dict[str, OpsJournal] = {}

    # -- lab lifecycle ---------------------------------------------------------

    async def build_lab(
        self, env: EnvSpec, backend_key: str | None = None
    ) -> LabHandle:
        logger.debug(
            "build_lab.entry",
            env=env.name,
            backend_key=backend_key,
            devices=len(env.devices),
        )
        selection = self._scheduler.select(env, backend_key)
        logger.debug("build_lab.backend_selected", backend_key=selection.backend_key, reason=selection.reason)
        backend = self._backends[selection.backend_key]
        lab = await backend.build(env)
        report = await backend.wait_ready(lab)
        if not report.passed:
            logger.debug("build_lab.not_ready", lab_id=lab.lab_id, summary=report.summary())
            await backend.destroy(lab)
            raise EnvError(
                f"lab for env {env.name!r} not ready: {report.summary()}",
                context={"env": env.name, "backend": selection.backend_key},
            )
        self._lab_backend[lab.lab_id] = selection.backend_key
        self._envs[lab.lab_id] = env
        snap0 = await backend.snapshot(lab, "snap0")
        self._snapshots[lab.lab_id] = [snap0]
        journal = OpsJournal(self.labs_dir, lab.lab_id)
        self._journals[lab.lab_id] = journal
        journal.append(
            {
                "record": "lab_build",
                "lab_id": lab.lab_id,
                "env": env.name,
                "backend": selection.backend_key,
                "reason": selection.reason,
                "ts": _utcnow_iso(),
            }
        )
        journal.append(
            {"record": "snapshot", "name": "snap0", "snapshot_id": snap0.snapshot_id}
        )
        logger.debug("build_lab.exit", lab_id=lab.lab_id, snap0=snap0.snapshot_id)
        return lab

    async def wait_ready(
        self, lab: LabHandle, timeout_s: int | None = None
    ) -> ReadyReport:
        return await self._backend(lab).wait_ready(lab, timeout_s or 600)

    async def snapshot(self, lab: LabHandle, name: str) -> SnapRef:
        logger.debug("snapshot.entry", lab_id=lab.lab_id, name=name)
        snap = await self._backend(lab).snapshot(lab, name)
        self._snapshots.setdefault(lab.lab_id, []).append(snap)
        self._journals[lab.lab_id].append(
            {"record": "snapshot", "name": name, "snapshot_id": snap.snapshot_id}
        )
        logger.debug("snapshot.exit", snapshot_id=snap.snapshot_id)
        return snap

    async def restore(self, lab: LabHandle, snap: SnapRef) -> None:
        logger.debug("restore.entry", lab_id=lab.lab_id, snapshot_id=snap.snapshot_id)
        await self._backend(lab).restore(lab, snap)
        self._journals[lab.lab_id].append(
            {"record": "restore", "snapshot_id": snap.snapshot_id, "ts": _utcnow_iso()}
        )
        logger.debug("restore.exit", lab_id=lab.lab_id)

    async def reset(self, lab: LabHandle) -> None:
        logger.debug("reset.entry", lab_id=lab.lab_id)
        await self._backend(lab).reset(lab)
        self._journals[lab.lab_id].append({"record": "reset", "ts": _utcnow_iso()})
        logger.debug("reset.exit", lab_id=lab.lab_id)

    async def destroy(self, lab: LabHandle) -> None:
        logger.debug("destroy.entry", lab_id=lab.lab_id)
        await self._backend(lab).destroy(lab)
        self._lab_backend.pop(lab.lab_id, None)
        self._envs.pop(lab.lab_id, None)
        self._snapshots.pop(lab.lab_id, None)
        self._journals.pop(lab.lab_id, None)
        logger.debug("destroy.exit", lab_id=lab.lab_id)

    # -- prepare / restore_task (ADR-20) ----------------------------------------

    async def prepare(
        self, lab: LabHandle, task_id: str, actions: list[PrepareAction]
    ) -> list[AppliedAction]:
        logger.debug(
            "prepare.entry", lab_id=lab.lab_id, task_id=task_id, actions=len(actions)
        )
        env = self._require_env(lab)
        applied: list[AppliedAction] = []
        for action in actions:
            if action.action == "exec_cli":
                applied.append(await self._prepare_exec_cli(lab, task_id, env, action))
            else:
                applied.append(await self._prepare_call_api(lab, task_id, env, action))
        logger.debug("prepare.exit", applied_count=len(applied))
        return applied

    async def _prepare_exec_cli(
        self,
        lab: LabHandle,
        task_id: str,
        env: EnvSpec,
        action: PrepareAction,
    ) -> AppliedAction:
        assert action.entries is not None
        backend = self._backend(lab)
        backups: dict[str, str] = {}
        for entry in action.entries:
            device = self._require_device(lab, env, entry.device)
            backup_text = backend.exec(lab, device, _capture_cmd(env, device))
            backup_dir = self.labs_dir / lab.lab_id / "backups" / task_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            path = backup_dir / f"{device}.cfg"
            path.write_text(backup_text, encoding="utf-8")
            backups[device] = str(path)
            for line in entry.cli_lines:
                output = backend.exec(lab, device, line)
                if output.startswith(_ERROR_OUTPUT_PREFIX):
                    raise EnvError(
                        f"prepare exec_cli failed on {device}: {line!r} -> {output}",
                        context={
                            "device": device,
                            "cli": line,
                            "output": output,
                            "task_id": task_id,
                        },
                    )
        applied = AppliedAction(
            action="exec_cli",
            entries=list(action.entries),
            backup_files=backups,
            applied_at=_utcnow_iso(),
        )
        self._journal(lab).append(
            {"record": "applied", "task_id": task_id, "applied": applied.model_dump()}
        )
        return applied

    async def _prepare_call_api(
        self,
        lab: LabHandle,
        task_id: str,
        env: EnvSpec,
        action: PrepareAction,
    ) -> AppliedAction:
        assert action.api is not None and action.params is not None
        await self._config_api.apply(env, action.api, action.params, undo=False)
        applied = AppliedAction(
            action="call_api",
            api=action.api,
            params=action.params,
            applied_at=_utcnow_iso(),
        )
        self._journal(lab).append(
            {"record": "applied", "task_id": task_id, "applied": applied.model_dump()}
        )
        return applied

    async def restore_task(self, lab: LabHandle, applied: list[AppliedAction]) -> None:
        logger.debug("restore_task.entry", lab_id=lab.lab_id, applied_count=len(applied))
        last: EnvError | None = None
        for attempt in range(_RESTORE_MAX_ATTEMPTS):
            try:
                await self._recover_once(lab, applied)
                self._journal(lab).append(
                    {"record": "restore_task", "ts": _utcnow_iso(), "status": "ok"}
                )
                logger.debug("restore_task.exit", attempt=attempt + 1, status="ok")
                return
            except EnvError as exc:
                last = exc
                logger.debug(
                    "restore_task.retry_round",
                    attempt=attempt + 1,
                    exc_type=type(exc).__name__,
                )
        self._journal(lab).append(
            {
                "record": "restore_task",
                "ts": _utcnow_iso(),
                "status": "failed",
                "error": str(last),
            }
        )
        assert last is not None
        logger.debug("restore_task.exit", status="failed", exc_type=type(last).__name__)
        raise last

    async def _recover_once(self, lab: LabHandle, applied: list[AppliedAction]) -> None:
        env = self._require_env(lab)
        backend = self._backend(lab)
        has_cli_restore = any(
            a.action == "exec_cli" and a.backup_files for a in applied
        )
        if has_cli_restore:
            # reset device state, then replay the task-pre backups (idempotent
            # SET semantics, DD-04 §5.4)
            await backend.reset(lab)
        for action in reversed(applied):
            if action.action == "call_api":
                assert action.api is not None and action.params is not None
                await self._config_api.apply(
                    env, action.api, action.params, undo=True
                )
            else:
                for device, path in action.backup_files.items():
                    backup = Path(path)
                    if not backup.is_file():
                        raise EnvError(
                            f"backup file {path!r} missing for device {device!r}",
                            context={"device": device, "path": path},
                        )
                    for line in backup.read_text(encoding="utf-8").splitlines():
                        if not line.strip() or line.startswith("#"):
                            continue
                        output = backend.exec(lab, device, line)
                        if output.startswith(_ERROR_OUTPUT_PREFIX):
                            raise EnvError(
                                f"backup replay failed on {device}: {line!r} -> {output}",
                                context={"device": device, "cli": line},
                            )
        report = await backend.wait_ready(lab)
        if not report.passed:
            raise EnvError(
                "post-restore readiness check failed",
                context={"lab_id": lab.lab_id, "report": report.summary()},
            )

    # -- read-only exec / health -------------------------------------------------

    def exec_readonly(self, lab: LabHandle, device: str, cli: str) -> str:
        cli_logged = cli[:200] if len(cli) > 200 else cli
        logger.debug("exec_readonly.entry", lab_id=lab.lab_id, device=device, cli=cli_logged)
        if not cli.strip().lower().startswith(READONLY_PREFIXES):
            logger.debug("exec_readonly.rejected", reason="not_readonly")
            raise DeviceError(
                f"cli {cli!r} is not in the read-only whitelist",
                context={"cli": cli, "device": device},
            )
        if device not in lab.endpoints:
            logger.debug("exec_readonly.rejected", reason="unknown_device", device=device)
            raise DeviceError(
                f"device {device!r} not present in lab {lab.lab_id!r}",
                context={"device": device, "lab_id": lab.lab_id},
            )
        try:
            output = self._backend(lab).exec(lab, device, cli)
        except EnvError as exc:
            logger.debug("exec_readonly.error", exc_type=type(exc).__name__)
            raise DeviceError(str(exc), context=exc.context) from exc
        logger.debug("exec_readonly.exit", output_len=len(output))
        return output

    async def health_check(self, lab: LabHandle, timeout_s: int = 5) -> bool:
        logger.debug("health_check.entry", lab_id=lab.lab_id, timeout_s=timeout_s)
        if not lab.endpoints:
            logger.debug("health_check.exit", healthy=False, reason="no_endpoints")
            return False
        device = next(iter(lab.endpoints))
        try:
            output = self._backend(lab).exec(lab, device, "display clock")
        except EnvError as exc:
            logger.debug("health_check.exit", healthy=False, exc_type=type(exc).__name__)
            return False
        healthy = "error" not in output.lower() and bool(output.strip())
        logger.debug("health_check.exit", healthy=healthy)
        return healthy

    async def backend_status(self) -> list[BackendStatus]:
        return [
            BackendStatus(
                key=key,
                kind=b.kind,
                active_labs=b.active_lab_count,
                available=True,
            )
            for key, b in self._backends.items()
        ]

    def snapshots(self, lab_id: str) -> list[SnapRef]:
        return list(self._snapshots.get(lab_id, []))

    # -- internals ----------------------------------------------------------------

    def _backend(self, lab: LabHandle) -> SandboxBackend:
        try:
            key = self._lab_backend[lab.lab_id]
        except KeyError:
            raise EnvError(
                f"lab {lab.lab_id!r} is not managed by this service",
                context={"lab_id": lab.lab_id},
            ) from None
        return self._backends[key]

    def _journal(self, lab: LabHandle) -> OpsJournal:
        try:
            return self._journals[lab.lab_id]
        except KeyError:
            raise EnvError(
                f"lab {lab.lab_id!r} has no ops journal",
                context={"lab_id": lab.lab_id},
            ) from None

    def _require_env(self, lab: LabHandle) -> EnvSpec:
        try:
            return self._envs[lab.lab_id]
        except KeyError:
            raise EnvError(
                f"lab {lab.lab_id!r} is not managed by this service",
                context={"lab_id": lab.lab_id},
            ) from None

    def _require_device(self, lab: LabHandle, env: EnvSpec, device: str) -> str:
        if device not in lab.endpoints:
            raise EnvError(
                f"prepare references unknown device {device!r}",
                context={"device": device, "lab_id": lab.lab_id, "env": env.name},
            )
        return device


def _capture_cmd(env: EnvSpec, device: str) -> str:
    vendor = next(d.type for d in env.devices if d.name == device)
    return (
        "display current-configuration"
        if vendor in ("vrp", "ce")
        else "show running-config"
    )
