"""SandboxService (DD-04 §4.1) — the seven-operation sandbox facade.

Orchestrates a backend adapter (lab lifecycle + device exec) with the
readiness check and an ops journal (``labs_dir/<lab_id>/ops.jsonl``) that
records every applied action for audit and L2 recovery replay. Recovery
follows ADR-20: L1 per-task (backup replay) with <=2 retries, then the
caller escalates to L2 (lab reset + snap0 restore). The call_api prepare
path was removed by ADR-27 (system-level NetworkPrepClient supersedes the
per-evalset ConfigApiCaller; the constructor injection is kept for
backward compatibility only).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
from rsi.sandbox.sshpool import DeviceSession, SessionKey, SshPool

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
# B-7: "output contains a timestamp -> healthy" (DD-04 §4 health_check).
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}:\d{2}")
# B-6: volatile line prefixes dropped by the config-equivalence normalization
_NORM_DROP_PREFIXES = ("#", "!", "<", "*", "%")
# R2-M2: ANCHORED device-timestamp line patterns. A line is dropped only
# when the whole line IS a device clock/banner line — a bare substring
# search would also swallow legitimate diff lines that merely contain a
# time-like token (e.g. a MAC address 00:11:22, or "uptime" inside a
# description). Patterns:
#   * full ISO timestamp line ("2026-01-01 00:00:00 UTC", with optional tz)
#   * "clock is ..." / "The current time is ..." device clock banner
#   * "... uptime is D days, H:MM:SS" banner line
_TS_LINE_RE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\s*[A-Za-z0-9+-]+)?"
    r"|(?:\S+\s+)?clock(?:\s+is)?\s+.*"
    r"|\S+ uptime(?:\s+is)?\s+\d+\s+\w+.*"
    r")$",
    re.IGNORECASE,
)


def normalize_config(text: str) -> list[str]:
    """B-6 equivalence-diff normalization: drop blank lines, comment/prompt
    lines and whole-line device timestamps, strip trailing whitespace."""
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_NORM_DROP_PREFIXES) or _TS_LINE_RE.match(stripped):
            continue
        kept.append(stripped)
    return kept


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
        settings=None,
        ssh_pool: SshPool | None = None,
        session_factory=None,
        readiness_checker=None,
    ) -> None:
        self._backends = backends
        self.labs_dir = Path(labs_dir)
        self._config_api = config_api or ConfigApiCaller()
        self._scheduler = scheduler or LabScheduler(backends)
        # settings wiring (H1): build retry limit + ready timeout
        self._build_retry = settings.sandbox.build_retry if settings else 2
        self._ready_timeout_s = (
            settings.sandbox.ready_timeout_s if settings else 600
        )
        self._lab_backend: dict[str, str] = {}
        self._envs: dict[str, EnvSpec] = {}
        self._snapshots: dict[str, list[SnapRef]] = {}
        self._journals: dict[str, OpsJournal] = {}
        # M1: device commands route through the SSH pool when available;
        # None keeps the pure-backend behavior (backward compatible).
        self._ssh_pool = ssh_pool
        # B-2: optional DB session factory — when wired, build/destroy persist
        # the labs row (device_endpoints_json is the ADR-24 authority).
        # B-15: optional ReadinessChecker — backend wait_ready fallback probe.
        self._session_factory = session_factory
        self._readiness_checker = readiness_checker
        self._closed = False

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
        last_error: EnvError | None = None
        lab = None
        for _attempt in range(self._build_retry + 1):
            lab = await backend.build(env)
            report = await backend.wait_ready(lab, self._ready_timeout_s)
            if report.passed:
                break
            logger.debug("build_lab.not_ready", lab_id=lab.lab_id, summary=report.summary())
            await backend.destroy(lab)
            last_error = EnvError(
                f"lab for env {env.name!r} not ready: {report.summary()}",
                context={"env": env.name, "backend": selection.backend_key},
            )
            lab = None
        if lab is None:
            assert last_error is not None
            raise last_error
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
        self._persist_lab_row(lab, env, selection.backend_key)
        logger.debug("build_lab.exit", lab_id=lab.lab_id, snap0=snap0.snapshot_id)
        return lab

    def _persist_lab_row(
        self, lab: LabHandle, env: EnvSpec, backend_key: str
    ) -> None:
        """B-2: INSERT the labs row (device_endpoints_json = ADR-24 authority).

        Best effort: a DB failure only logs a warning — lab lifecycle must
        never fail because of metadata persistence."""
        if self._session_factory is None:
            return
        try:
            from rsi.db.tables import Lab

            env_json = json.dumps(
                env.model_dump(mode="json"), sort_keys=True, default=str
            )
            endpoints = {
                name: {"ip": a.ip, "port": a.port}
                for name, a in lab.endpoints.items()
            }
            with self._session_factory() as session:
                session.add(
                    Lab(
                        id=lab.lab_id,
                        host=lab.host,
                        backend=backend_key,
                        env_hash=hashlib.sha256(env_json.encode()).hexdigest()[:16],
                        status="ready",
                        online=1,
                        devices_online=f"{len(lab.endpoints)}/{len(env.devices)}",
                        device_endpoints_json=json.dumps(endpoints),
                        created_at=_utcnow_iso(),
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - metadata write must not fail builds
            logger.warning("labs.persist_failed", lab_id=lab.lab_id, error=str(exc))

    def _mark_lab_destroyed(self, lab_id: str) -> None:
        """B-2/IN-015: UPDATE labs SET status='destroyed', destroyed_at=now."""
        if self._session_factory is None:
            return
        try:
            from sqlalchemy import update
            from rsi.db.tables import Lab

            with self._session_factory() as session:
                session.exec(
                    update(Lab)
                    .where(Lab.id == lab_id)
                    .values(status="destroyed", destroyed_at=_utcnow_iso())
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("labs.destroy_mark_failed", lab_id=lab_id, error=str(exc))

    async def wait_ready(
        self, lab: LabHandle, timeout_s: int | None = None
    ) -> ReadyReport:
        """Backend wait_ready with a ReadinessChecker fallback (B-15).

        When the backend report fails and a checker is wired, one extra
        checker round (ssh_reachable / cli_match) runs before giving up —
        real backends (S11) may report false negatives while the devices are
        already reachable."""
        timeout = timeout_s or self._ready_timeout_s
        report = await self._backend(lab).wait_ready(lab, timeout)
        if report.passed or self._readiness_checker is None:
            return report
        env = self._envs.get(lab.lab_id)
        if env is None:
            return report
        try:
            fallback = await self._readiness_checker.check(
                env, lab.endpoints, timeout_s=10
            )
        except Exception as exc:  # noqa: BLE001 - probe must not raise
            logger.debug(
                "wait_ready.checker_failed", exc_type=type(exc).__name__
            )
            return report
        if fallback.passed:
            logger.debug("wait_ready.checker_passed", lab_id=lab.lab_id)
            return fallback
        return report

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
        self._mark_lab_destroyed(lab.lab_id)
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
            applied.append(await self._prepare_exec_cli(lab, task_id, env, action))
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
            # E2 实测：配置批必须整体进系统视图（netmiko send_config_set）；
            # 逐行用户视图下发配置命令在真实 VRP 上必失败。池化会话不可用
            # 时保持 backend.exec 逐行（Fake 后端语义）。
            session = self._pool_session(lab, device)
            if session is not None:
                result = _run_sync(session.exec_config(list(entry.cli_lines)))
                if not result.ok:
                    raise EnvError(
                        f"prepare exec_cli failed on {device}: "
                        f"{entry.cli_lines} -> {result.matched_errors or result.output[:120]}",
                        context={
                            "device": device,
                            "cli": "; ".join(entry.cli_lines),
                            "output": result.output[:400],
                            "task_id": task_id,
                        },
                    )
            else:
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

    async def replay_applied_ops(
        self, lab: LabHandle, *, exclude_task_ids: set[str] | None = None
    ) -> int:
        """B-24: L2 ops-journal replay — re-apply journalled prepare actions.

        Reads the lab's ops.jsonl (``OpsJournal.read``) and re-sends every
        ``applied`` record's CLI lines to the recorded device; ``call_api``
        records are skipped with a warning (config-API writes are not
        idempotent and the snap0 restore already carries the API-side state).
        Records whose ``task_id`` is in ``exclude_task_ids`` are skipped (the
        failing task's own prepare is exactly what L2 is recovering from).

        Returns the number of re-applied CLI lines; raises EnvError on the
        first device error so the caller can degrade the task to env_failed.
        """
        exclude = set(exclude_task_ids or ())
        env = self._require_env(lab)
        backend = self._backend(lab)
        records = self._journal(lab).read()
        replayed = 0
        for rec in records:
            if rec.get("record") != "applied":
                continue
            if str(rec.get("task_id", "")) in exclude:
                continue
            applied = rec.get("applied") or {}
            if applied.get("action") != "exec_cli":
                logger.warning(
                    "sandbox.replay_skip_call_api",
                    lab_id=lab.lab_id,
                    task_id=rec.get("task_id"),
                    api=applied.get("api"),
                )
                continue
            for entry in applied.get("entries") or []:
                device = entry.get("device")
                self._require_device(lab, env, device)
                for line in entry.get("cli_lines") or []:
                    output = backend.exec(lab, device, line)
                    if output.startswith(_ERROR_OUTPUT_PREFIX):
                        raise EnvError(
                            f"journal replay failed on {device}: {line!r} -> {output}",
                            context={
                                "device": device,
                                "cli": line,
                                "lab_id": lab.lab_id,
                                "task_id": rec.get("task_id"),
                            },
                        )
                    replayed += 1
        self._journal(lab).append(
            {
                "record": "l2_replay",
                "ts": _utcnow_iso(),
                "replayed": replayed,
                "excluded": sorted(exclude),
            }
        )
        logger.debug(
            "sandbox.replay_applied_ops.exit", lab_id=lab.lab_id, replayed=replayed
        )
        return replayed

    async def _recover_once(self, lab: LabHandle, applied: list[AppliedAction]) -> None:
        env = self._require_env(lab)
        backend = self._backend(lab)
        has_cli_restore = any(
            a.action == "exec_cli" and a.backup_files for a in applied
        )
        devices_to_reset: list[str] = []
        if has_cli_restore:
            devices_to_reset = sorted(
                {d for a in applied if a.backup_files for d in a.backup_files}
            )
            pool_sessions = {
                d: self._pool_session(lab, d) for d in devices_to_reset
            }
            if all(s is not None for s in pool_sessions.values()):
                # B-6 (DD-04 §5.4): per-device vendor reset_cmd over the SSH
                # pool instead of a whole-lab reset — only the devices touched
                # by this task's prepare are rolled back.
                for device, session in pool_sessions.items():
                    result = _run_sync(
                        session.exec_config(list(session.vendor.reset_cmd))
                    )
                    if not result.ok:
                        raise EnvError(
                            f"vendor reset_cmd failed on {device}: "
                            f"{result.matched_errors}",
                            context={"device": device, "lab_id": lab.lab_id},
                        )
            else:
                # no pooled session for every device — legacy whole-lab reset
                await backend.reset(lab)
        for action in reversed(applied):
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
                    session = self._pool_session(lab, device)
                    if session is not None:
                        result = _run_sync(session.exec_config([line]))
                        if not result.ok:
                            raise EnvError(
                                f"backup replay failed on {device}: {line!r} "
                                f"-> {result.matched_errors}",
                                context={"device": device, "cli": line},
                            )
                    else:
                        output = backend.exec(lab, device, line)
                        if output.startswith(_ERROR_OUTPUT_PREFIX):
                            raise EnvError(
                                f"backup replay failed on {device}: {line!r} -> {output}",
                                context={"device": device, "cli": line},
                            )
        # B-6: post-replay equivalence spot check — the captured running
        # config must match the backup (normalized) or the restore counts as
        # failed and the retry path runs.
        if has_cli_restore:
            for device in devices_to_reset:
                session = self._pool_session(lab, device)
                if session is None:
                    continue
                action = next(
                    a for a in applied if a.backup_files and device in a.backup_files
                )
                backup_text = Path(action.backup_files[device]).read_text(
                    encoding="utf-8"
                )
                captured = _run_sync(session.capture_config())
                if normalize_config(captured) != normalize_config(backup_text):
                    logger.warning(
                        "restore_task.equivalence_mismatch",
                        device=device,
                        lab_id=lab.lab_id,
                    )
                    raise EnvError(
                        f"post-restore config mismatch on {device}",
                        context={"device": device, "lab_id": lab.lab_id},
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
        session = self._pool_session(lab, device)
        if session is not None:
            result = _run_sync(session.exec_cli(cli))
            logger.debug(
                "exec_readonly.pool_exit", ok=result.ok, output_len=len(result.output)
            )
            if not result.ok:
                raise DeviceError(
                    f"cli {cli!r} failed on {device!r} via ssh session",
                    context={
                        "device": device,
                        "lab_id": lab.lab_id,
                        "cli": cli,
                        "matched_errors": result.matched_errors,
                        "output": result.output,
                    },
                )
            return result.output
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
        session = self._pool_session(lab, device)
        if session is not None:
            # H1: run the probe on the runner loop via the same bridge as
            # exec_readonly — awaiting a pooled session coroutine on the
            # caller's loop would bind DeviceSession.lock to that loop and
            # later collide with the runner loop ("bound to a different
            # event loop").
            try:
                result = _run_sync(session.exec_cli("display clock"))
            except Exception as exc:  # noqa: BLE001 — probe must not raise
                logger.debug("health_check.exit", healthy=False, exc_type=type(exc).__name__)
                return False
            healthy = result.ok and bool(_TS_RE.search(result.output))
            logger.debug("health_check.exit", healthy=healthy, via="ssh_pool")
            return healthy
        try:
            output = self._backend(lab).exec(lab, device, "display clock")
        except EnvError as exc:
            logger.debug("health_check.exit", healthy=False, exc_type=type(exc).__name__)
            return False
        healthy = bool(_TS_RE.search(output))
        logger.debug("health_check.exit", healthy=healthy)
        return healthy

    async def backend_status(self) -> list[BackendStatus]:
        """Aggregate every backend's ``status()`` (T4).

        Backends whose default ``status()`` leaves ``key`` empty (e.g. the
        Fake) get the registry key filled in so callers see configuration
        keys, not blank identifiers.
        """
        statuses: list[BackendStatus] = []
        for key, b in self._backends.items():
            st = b.status()
            if not st.key:
                st = st.model_copy(update={"key": key})
            statuses.append(st)
        return statuses

    def snapshots(self, lab_id: str) -> list[SnapRef]:
        return list(self._snapshots.get(lab_id, []))

    def close(self) -> None:
        """Reclaim pooled SSH sessions and the owned config-api client.

        Idempotent: subsequent calls are no-ops. Each step is closed
        independently: a failure in one (logged, not raised) never leaves
        the other unclosed. Note: cross-loop ``aclose`` of an idle client
        is a known S11 concern, tracked for the real executor phase.
        """
        if self._closed:
            return
        self._closed = True
        if self._ssh_pool is not None:
            try:
                _run_sync(self._ssh_pool.close_all())
            except Exception as exc:  # noqa: BLE001 — close must not raise
                logger.warning("close.ssh_pool_failed", error=str(exc))
        try:
            _run_sync(self._config_api.aclose())
        except Exception as exc:  # noqa: BLE001 — close must not raise
            logger.warning("close.config_api_failed", error=str(exc))

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

    def _pool_session(self, lab: LabHandle, device: str) -> DeviceSession | None:
        """Resolve a pooled SSH session for the device, or None to fall back.

        M1 (DD-04 §4.1/4.2): when a SshPool is wired and the device ssh
        context is constructible (endpoint address + known vendor), device
        commands go through the pooled session (connection reuse, per-session
        lock, alive probing). Otherwise the backend path is unchanged.
        """
        if self._ssh_pool is None:
            return None
        access = lab.endpoints.get(device)
        if access is None:
            return None
        env = self._envs.get(lab.lab_id)
        vendor = (
            next((d.type for d in env.devices if d.name == device), None)
            if env is not None
            else None
        )
        if vendor is None:
            return None
        key = SessionKey(ip=access.ip, port=access.port, lab_id=lab.lab_id)
        return _run_sync(self._ssh_pool.device(key, access, vendor))


class _LoopRunner:
    """One daemon thread with one event loop for sync-initiated coroutines.

    Keeps all pool coroutines invoked from sync code (exec_readonly/close)
    on a single loop, so session locks never hop between event loops.
    """

    # F-3 (audit3): the bridge wait must comfortably exceed the device-side
    # read timeout (DeviceSession.exec_cli defaults to 120s) — a 30s wait
    # fired first, and the bare TimeoutError escaped the EnvError handling
    # in restore_task/run_task, killing the whole batch instead of grading
    # the task env_failed. 180s = 120s device read timeout + retry headroom.
    DEFAULT_TIMEOUT_S = 180.0

    def __init__(self, timeout_s: float | None = None) -> None:
        import threading

        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.timeout_s = self.DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s

    def run(self, coro):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                import threading as _t

                _t.Thread(
                    target=self._loop.run_forever, daemon=True, name="ssh-pool-loop"
                ).start()
        try:
            # L1: bounded wait — a hung runner thread must not deadlock the
            # caller forever.
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
                timeout=self.timeout_s
            )
        except asyncio.TimeoutError as exc:
            # F-3: EnvError (not the builtin TimeoutError) so the existing
            # task-level degradation in BatchRunner.run_task classifies this
            # as env_failed instead of aborting the batch.
            raise EnvError(
                "sandbox runner loop coroutine did not finish within "
                f"{self.timeout_s}s",
                context={"timeout_s": self.timeout_s, "reason": "runner_bridged_timeout"},
            ) from exc


_RUNNER = _LoopRunner()


def _run_sync(coro):
    """Run a coroutine to completion from sync code (safe inside async too)."""
    return _RUNNER.run(coro)


def _capture_cmd(env: EnvSpec, device: str) -> str:
    vendor = next(d.type for d in env.devices if d.name == device)
    return (
        "display current-configuration"
        if vendor in ("vrp", "ce")
        else "show running-config"
    )
