"""HostService: instance lifecycle over HostPool + NetPolicy + CarrierAdapter.

P0 offline: no GitStore export (config_version_id doubles as the config dir
path placeholder), no real containers — adapter/fake drives everything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rsi.carrier.adapters.fake import CarrierAdapter, RunHandle
from rsi.carrier.extract import extract_task_result
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.scheduler import InstanceScheduler
from rsi.carrier.overlay import OverlayBuilder
from rsi.core.models import LabHandle, TaskResult, TaskSpec
from rsi.core.naming import new_id
from rsi.log import get_logger

__all__ = ["HostService", "ImageCheck", "InstanceHandle", "RunHandle"]

logger = get_logger(__name__)


class InstanceHandle(BaseModel):
    """A live SUT instance managed by HostService."""

    instance_id: str
    sut_id: str
    run_id: str
    host: str
    container_id: str
    status: str = "running"
    config_dir: Path
    env: dict[str, str] = Field(default_factory=dict)
    # B-18: carrier type this instance was created for — adapter dispatch is
    # keyed on this, not on the execution host.
    carrier: str = "fake"


class ImageCheck(BaseModel):
    """Result of a SUT image verification probe (G5 / DD-05 §4).

    ``ok=None`` means "unknown/offline" — the Docker daemon was unreachable
    so no check could be performed (P0 offline mode)."""

    ok: bool | None
    image_ref: str
    checks: list[str] = Field(default_factory=list)
    detail: str = ""


def build_dev_env(cred: Any) -> dict[str, str]:
    """Derive DEV_<DEV>_* / OPENCODE_API_KEY env vars from a cred payload (M3).

    Accepted shape: ``{"api_key": ..., "devices": {"R1": {"user": ...,
    "pass": ..., "host": ..., "port": ...}}}`` (CredentialResolver output).
    Missing keys are skipped; unknown cred types yield an empty env.
    """
    if not isinstance(cred, dict):
        return {}
    env: dict[str, str] = {}
    devices = cred.get("devices")
    if isinstance(devices, dict):
        for name, dev in devices.items():
            if not isinstance(dev, dict):
                continue
            prefix = f"DEV_{str(name).upper()}_"
            for key, suffix in (
                ("user", "USER"),
                ("pass", "PASS"),
                ("host", "HOST"),
                ("port", "PORT"),
            ):
                if dev.get(key) is not None:
                    env[f"{prefix}{suffix}"] = str(dev[key])
    if cred.get("api_key") is not None:
        env["OPENCODE_API_KEY"] = str(cred["api_key"])
    return env


class HostService:
    """Instance create/submit/collect/destroy over pluggable carriers."""

    def __init__(
        self,
        host_pool: HostPool,
        net_policy: NetPolicy,
        adapters: dict[str, CarrierAdapter],
        scheduler: InstanceScheduler | None = None,
        settings=None,
    ) -> None:
        if not adapters:
            raise ValueError("at least one CarrierAdapter is required")
        self.host_pool = host_pool
        self.net_policy = net_policy
        # settings wiring (H1): scheduler default from carrier.max_instances_per_host
        default_max = (
            settings.carrier.max_instances_per_host if settings is not None else 10
        )
        self.scheduler = scheduler or InstanceScheduler(host_pool, default_max)
        self.adapters = adapters
        self._default_adapter = next(iter(adapters.values()))
        self._instances: dict[str, InstanceHandle] = {}

    def _adapter_for(self, carrier_type: str) -> CarrierAdapter:
        """B-18: adapters are keyed by carrier type, not by execution host."""
        return self.adapters.get(carrier_type, self._default_adapter)

    def create_instance(
        self,
        image_ref: str,
        config_version_id: str,
        lab: LabHandle,
        permission: str,
        instrument: bool,
        host: str | None = None,
        cred: Any = None,
        run_id: str = "",
        carrier_type: str = "fake",
    ) -> InstanceHandle:
        """Create (P0: register) a SUT instance attached to lab + trace nets.

        ``host=None`` → auto-select least-loaded host via InstanceScheduler.
        ``carrier_type`` selects the CarrierAdapter (B-18); the host only
        affects the Docker pool and network placement.
        """
        logger.debug(
            "create_instance.entry",
            image_ref=image_ref,
            lab_id=lab.lab_id,
            instrument=instrument,
            host=host,
            carrier_type=carrier_type,
        )
        selected_host = self.scheduler.select(host)
        logger.debug("create_instance.host_selected", host=selected_host)
        # Touch the host's Docker client (P0: offline pool returns None;
        # adapter-driven mode tolerates a daemon-less dev environment).
        try:
            self.host_pool.client(selected_host)
        except Exception:
            pass
        lab_net = self.net_policy.ensure_lab_net(selected_host, lab.lab_id)
        trace_net = self.net_policy.ensure_trace_net(selected_host)
        instance_id = new_id("inst")
        # P0: skip GitStore export — config_version_id is a placeholder path.
        # Reject empty/placeholder ids: ``Path("")`` is ``.`` (cwd), and
        # overlaying cwd would recursively copy the whole working tree.
        config_dir = (
            Path(config_version_id)
            if str(config_version_id).strip()
            else Path("<unversioned>")
        )
        overlay_built = False
        if instrument and config_dir.is_dir():
            logger.debug("create_instance.overlay", source=str(config_dir))
            config_dir = OverlayBuilder.build(config_dir)
            overlay_built = True
        handle = InstanceHandle(
            instance_id=instance_id,
            sut_id=image_ref,
            run_id=run_id,
            host=selected_host,
            container_id=instance_id,
            status="running",
            config_dir=config_dir,
            env={"RSI_LAB_ID": lab.lab_id, "RSI_RUN_ID": run_id, **build_dev_env(cred)},
            carrier=carrier_type,
        )
        # B-9: DD-05 §5.3 — a failure after the overlay exists must clean up
        # the half-built container + directory and roll the host count back.
        try:
            self.net_policy.attach(
                selected_host, handle.container_id, [lab_net, trace_net]
            )
            self.host_pool.increment(selected_host)
            self._instances[instance_id] = handle
        except Exception:
            logger.warning(
                "create_instance.cleanup_after_failure", instance_id=instance_id
            )
            try:
                self.net_policy.detach(handle.container_id)
            except Exception:  # noqa: BLE001 - best effort during cleanup
                pass
            if overlay_built:
                try:
                    OverlayBuilder.cleanup(config_dir)
                except Exception:  # noqa: BLE001 - best effort during cleanup
                    pass
            raise
        logger.debug("create_instance.exit", instance_id=instance_id, host=selected_host)
        return handle

    async def verify_image(
        self, image_ref: str, host: str | None = None
    ) -> ImageCheck:
        """G5 (DD-05 §4): check the SUT image exists on a host.

        P0 offline: when the Docker daemon is unreachable (or the pool is the
        offline stub) the result is ``ok=None`` — unknown, not failed.
        """
        logger.debug("verify_image.entry", image_ref=image_ref, host=host)
        selected = host or "main"
        try:
            client = self.host_pool.client(selected)
        except Exception as exc:  # noqa: BLE001 - offline/unconfigured host
            logger.debug("verify_image.exit", ok=None, reason="no_client")
            return ImageCheck(
                ok=None,
                image_ref=image_ref,
                detail=f"offline: docker daemon unreachable ({type(exc).__name__})",
            )
        if client is None:
            logger.debug("verify_image.exit", ok=None, reason="offline_stub")
            return ImageCheck(
                ok=None,
                image_ref=image_ref,
                detail="offline: docker daemon unreachable",
            )
        try:
            await asyncio.to_thread(client.images.get, image_ref)
        except Exception as exc:  # noqa: BLE001 - image missing / API error
            logger.debug("verify_image.exit", ok=False, exc_type=type(exc).__name__)
            return ImageCheck(
                ok=False,
                image_ref=image_ref,
                checks=["docker inspect"],
                detail=f"image not found: {exc}",
            )
        logger.debug("verify_image.exit", ok=True)
        return ImageCheck(
            ok=True, image_ref=image_ref, checks=["docker inspect"], detail=""
        )

    def submit_task(self, instance: InstanceHandle, task: TaskSpec) -> RunHandle:
        """Dispatch the task prompt to the instance's carrier adapter."""
        logger.debug(
            "submit_task.entry",
            instance_id=instance.instance_id,
            prompt_len=len(task.prompt),
            tool_constraints=len(task.tool_constraints),
        )
        adapter = self._adapter_for(instance.carrier)
        result = adapter.submit_task(instance, task)
        logger.debug("submit_task.exit", result_type=type(result).__name__)
        return result

    def collect(self, handle: RunHandle, timeout_s: float = 1800) -> TaskResult:
        """Collect stdout and extract the FINAL_JSON result."""
        logger.debug("collect.entry", instance_id=handle.instance_id, timeout_s=timeout_s)
        instance = self._instances.get(handle.instance_id)
        adapter = (
            self._adapter_for(instance.carrier) if instance else self._default_adapter
        )
        try:
            stdout = adapter.collect_result(handle, timeout_s)
        except Exception as exc:
            logger.debug("collect.instance_crashed", exc_type=type(exc).__name__)
            return TaskResult(task_complete=False, instance_crashed=True)
        # B-10 (DD-05 §4): container exit code semantics. 137 (OOM-kill) /
        # 139 (segfault) mean the run infra-crashed — FINAL_JSON extraction is
        # meaningless. exit_code==0 with empty stdout is a parse error.
        exit_code = getattr(handle, "exit_code", None)
        if exit_code in (137, 139):
            logger.debug(
                "collect.instance_crashed", exit_code=exit_code, via="exit_code"
            )
            return TaskResult(task_complete=False, instance_crashed=True)
        if exit_code == 0 and not (stdout or "").strip():
            logger.debug("collect.parse_error", reason="empty_stdout", exit_code=0)
            return TaskResult(task_complete=False, parse_error=True)
        result = extract_task_result(stdout)
        logger.debug(
            "collect.exit",
            task_complete=result.task_complete,
            parse_error=result.parse_error,
        )
        return result

    def trace_channels(self, instance: InstanceHandle) -> Any:
        """Adapter passthrough: locate the carrier's trace output streams.

        F1.2 (audit4): consumed by BatchRunner after collect to tail wiretap
        ndjson files into the TraceCollector.
        """
        return self._adapter_for(instance.carrier).trace_channels(instance)

    def destroy(self, instance: InstanceHandle) -> None:
        """Detach networks, release the host slot and stop tracking."""
        logger.debug("destroy.entry", instance_id=instance.instance_id, host=instance.host)
        self.net_policy.detach(instance.container_id)
        self.host_pool.decrement(instance.host)
        self._instances.pop(instance.instance_id, None)
        logger.debug("destroy.exit", instance_id=instance.instance_id)

    def destroy_stale(self, host: str) -> int:
        """Clean up leftover instances on a host; returns count removed."""
        stale = [i for i in self._instances.values() if i.host == host]
        for instance in stale:
            self.destroy(instance)
        return len(stale)
