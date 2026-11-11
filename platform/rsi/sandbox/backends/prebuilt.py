"""PrebuiltBackend (T3, DD-04 §5.7) — prebuilt-mode sandbox lifecycle.

eNSP Pro / PNETLab platforms cannot be driven over SSH by the platform
itself; the lab topology is pre-built manually in the platform's web UI.
This backend therefore:

* ``build``    — only verifies the platform URL probe is online, then passes
                 the env's per-device ``ssh_access`` endpoints through as the
                 LabHandle (nothing is provisioned);
* lifecycle    — snapshot/restore are no-ops (L1 config backup replay covers
                 task recovery; L2 degradation is handled by ``reset``);
* ``reset``    — per-device vendor ``reset_cmd`` over the shared SshPool;
* ``exec``     — device-level SSH via the same shared SshPool SandboxService
                 uses ((ip, port) key — no double layer);
* ``status``   — available = platform URL probe ok.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from rsi.core.errors import EnvError
from rsi.log import get_logger
from rsi.core.models import EnvSpec, LabHandle, SnapRef
from rsi.core.naming import new_id
from rsi.sandbox.backends.base import BackendStatus, SandboxBackend
from rsi.sandbox.readiness import ReadyReport
from rsi.sandbox.service import _run_sync
from rsi.sandbox.sshpool import SessionKey, SshPool
from rsi.sandbox.vendor import VENDOR_MAP
from rsi.settings import DeviceEndpoint

__all__ = ["PrebuiltBackend"]

logger = get_logger(__name__)


class PrebuiltBackend(SandboxBackend):
    """Prebuilt-mode backend shared by ensp-pro / pnetlab."""

    def __init__(
        self,
        key: str,
        kind: str,
        probe,
        ssh_pool: SshPool | None,
        readiness,
        reset_cmds: dict[str, list[str]] | None = None,
        devices: dict[str, DeviceEndpoint] | None = None,
    ) -> None:
        self._key = key
        self.kind = kind
        self._probe = probe
        self._pool = ssh_pool
        self._readiness = readiness
        # 平台级设备清单（settings ``sandbox.backends.<key>.devices``）：
        # 仅监控寻址（ip/port）；凭证属评测集 env.yaml（ADR-25 职责划分）。
        # 物理网络平台（无 URL）的在线判定与在用 lab 无关——任一设备
        # SSH 可达即平台在线。
        self._devices = dict(devices or {})
        # optional per-vendor override of the reset command table; defaults
        # to the VendorProfile SSOT (same source as SandboxService B-6).
        self._reset_cmds = reset_cmds or {}
        self._envs: dict[str, EnvSpec] = {}

    # -- lifecycle -----------------------------------------------------------

    async def build(self, env: EnvSpec) -> LabHandle:
        logger.debug("build.entry", env=env.name, devices=len(env.devices), kind=self.kind)
        # 未配置平台 URL（物理网络）无平台层可探——build 仅透传设备端点，
        # 设备可达性由 wait_ready / status 的设备 SSH 判定承担。
        if self._probe.url:
            result = self._probe.probe_cached()
            if not result.ok:
                logger.debug(
                    "build.error", exc_type="EnvError", url=self._probe.url, detail=result.detail
                )
                raise EnvError(
                    f"platform {self._probe.url!r} is offline: {result.detail}",
                    context={
                        "url": self._probe.url,
                        "detail": result.detail,
                        "backend": self._key,
                    },
                )
        lab_id = new_id("lab")
        self._envs[lab_id] = env
        logger.debug("build.exit", lab_id=lab_id)
        return LabHandle(
            lab_id=lab_id,
            host=_url_host(self._probe.url),
            backend=self.kind,
            endpoints=dict(env.ssh_access),
        )

    async def wait_ready(self, lab: LabHandle, timeout_s: int = 600) -> ReadyReport:
        logger.debug("wait_ready.entry", lab_id=lab.lab_id, timeout_s=timeout_s)
        env = self._require_env(lab)
        return await self._readiness.check(env, lab.endpoints, timeout_s)

    async def snapshot(self, lab: LabHandle, name: str) -> SnapRef:
        logger.debug("snapshot.entry", lab_id=lab.lab_id, name=name)
        self._require_env(lab)
        return SnapRef(snapshot_id=new_id("snap"), impl_kind="noop", backend_ref=name)

    async def restore(self, lab: LabHandle, snap: SnapRef) -> None:
        logger.debug("restore.entry", lab_id=lab.lab_id, snapshot_id=snap.snapshot_id)
        self._require_env(lab)
        logger.warning(
            "prebuilt mode: snapshot restore is a no-op; "
            "L1 config backup restore covers task recovery"
        )

    async def reset(self, lab: LabHandle) -> None:
        logger.debug("reset.entry", lab_id=lab.lab_id)
        env = self._require_env(lab)
        if self._pool is None:
            logger.warning(
                "prebuilt mode: no ssh pool wired; reset skipped", lab_id=lab.lab_id
            )
            return
        for device in env.devices:
            access = env.ssh_access.get(device.name)
            if access is None:
                logger.warning(
                    "prebuilt mode: no ssh_access for device; reset skipped",
                    lab_id=lab.lab_id,
                    device=device.name,
                )
                continue
            key = SessionKey(ip=access.ip, port=access.port, lab_id=lab.lab_id)
            session = _run_sync(self._pool.device(key, access, device.type))
            cmds = self._reset_cmds_for(device.type)
            result = _run_sync(session.exec_config(cmds))
            if not result.ok:
                logger.debug(
                    "reset.error", device=device.name, matched=result.matched_errors
                )
                raise EnvError(
                    f"vendor reset_cmd failed on {device.name}: {result.matched_errors}",
                    context={"device": device.name, "lab_id": lab.lab_id},
                )
        logger.debug("reset.exit", lab_id=lab.lab_id)

    async def destroy(self, lab: LabHandle) -> None:
        logger.debug("destroy.entry", lab_id=lab.lab_id)
        self._envs.pop(lab.lab_id, None)
        if self._pool is not None:
            _run_sync(self._pool.close_lab(lab.lab_id))
        logger.debug("destroy.exit", lab_id=lab.lab_id)

    # -- exec ------------------------------------------------------------------

    def exec(self, lab: LabHandle, device: str, cli: str) -> str:
        logger.debug("exec.entry", lab_id=lab.lab_id, device=device, cli=cli[:200])
        env = self._require_env(lab)
        if self._pool is None:
            raise EnvError(
                f"no ssh pool wired on backend {self._key!r}; cannot exec",
                context={"lab_id": lab.lab_id, "device": device},
            )
        access = lab.endpoints.get(device)
        if access is None:
            raise EnvError(
                f"device {device!r} not present in lab {lab.lab_id!r}",
                context={"device": device, "lab_id": lab.lab_id},
            )
        vendor = next(
            (d.type for d in env.devices if d.name == device), None
        )
        if vendor is None:
            raise EnvError(
                f"device {device!r} has no vendor in env {env.name!r}",
                context={"device": device, "lab_id": lab.lab_id},
            )
        # same (ip, port) key semantics as SandboxService._pool_session —
        # the pool deduplicates on (ip, port) so the two paths share sessions
        key = SessionKey(ip=access.ip, port=access.port, lab_id=lab.lab_id)
        try:
            session = _run_sync(self._pool.device(key, access, vendor))
            result = _run_sync(session.exec_cli(cli))
        except EnvError:
            raise
        except Exception as exc:  # noqa: BLE001 - session failure -> EnvError
            logger.debug("exec.error", exc_type=type(exc).__name__, device=device)
            raise EnvError(
                f"ssh session failed on {device} ({access.ip}:{access.port}): {exc}",
                context={"device": device, "lab_id": lab.lab_id},
            ) from exc
        if not result.ok:
            raise EnvError(
                f"cli {cli!r} failed on {device}: {result.matched_errors}",
                context={
                    "device": device,
                    "lab_id": lab.lab_id,
                    "cli": cli,
                    "output": result.output,
                },
            )
        logger.debug("exec.exit", output_len=len(result.output))
        return result.output

    # -- introspection ----------------------------------------------------------

    @property
    def active_lab_count(self) -> int:
        return len(self._envs)

    def status(self) -> BackendStatus:
        # ADR-25 增补：配置了 URL → URL 探活；未配置 URL（物理网络）→
        # ① 平台级设备清单（settings devices，与在用 lab 无关）任一设备
        #    SSH 可达即在线；② 平台级为空 → 回退任一在用 lab 设备端点
        #    可达即在线（同步 TCP 探测，3s 超时，语义同 readiness 的
        #    ssh_reachable）。
        if self._probe.url:
            result = self._probe.probe_cached()
            return BackendStatus(
                key=self._key,
                kind=self.kind,
                active_labs=len(self._envs),
                available=result.ok,
                detail=result.detail,
            )
        for ep in self._devices.values():
            if tcp_reachable(ep.ip, ep.port):
                return BackendStatus(
                    key=self._key,
                    kind=self.kind,
                    active_labs=len(self._envs),
                    available=True,
                    detail="设备 SSH 可达（平台配置）",
                )
        for env in self._envs.values():
            for access in env.ssh_access.values():
                if tcp_reachable(access.ip, access.port):
                    return BackendStatus(
                        key=self._key,
                        kind=self.kind,
                        active_labs=len(self._envs),
                        available=True,
                        detail="设备 SSH 可达",
                    )
        return BackendStatus(
            key=self._key,
            kind=self.kind,
            active_labs=len(self._envs),
            available=False,
            detail="未配置 URL 且无设备信息",
        )

    # -- internals ----------------------------------------------------------------

    def _require_env(self, lab: LabHandle) -> EnvSpec:
        try:
            return self._envs[lab.lab_id]
        except KeyError:
            raise EnvError(
                f"unknown lab {lab.lab_id!r} in prebuilt backend {self._key!r}",
                context={"lab_id": lab.lab_id},
            ) from None

    def _reset_cmds_for(self, vendor: str) -> list[str]:
        override = self._reset_cmds.get(vendor)
        if override is not None:
            return list(override)
        return list(VENDOR_MAP[vendor].reset_cmd)


def _url_host(url: str) -> str:
    return urlparse(url).hostname or url


DEVICE_SSH_TIMEOUT_S = 3.0


def tcp_reachable(ip: str, port: int) -> bool:
    """Synchronous TCP connect probe (mirrors readiness ssh_reachable, 3s)."""
    try:
        with socket.create_connection((ip, port), timeout=DEVICE_SSH_TIMEOUT_S):
            return True
    except OSError:
        return False
