"""HostService: instance lifecycle over HostPool + NetPolicy + CarrierAdapter.

P0 offline: no GitStore export (config_version_id doubles as the config dir
path placeholder), no real containers — adapter/fake drives everything.
"""

from __future__ import annotations

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

__all__ = ["HostService", "InstanceHandle", "RunHandle"]

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

    def _adapter_for(self, host: str) -> CarrierAdapter:
        return self.adapters.get(host, self._default_adapter)

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
    ) -> InstanceHandle:
        """Create (P0: register) a SUT instance attached to lab + trace nets.

        ``host=None`` → auto-select least-loaded host via InstanceScheduler.
        """
        logger.debug(
            "create_instance.entry",
            image_ref=image_ref,
            lab_id=lab.lab_id,
            instrument=instrument,
            host=host,
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
        config_dir = Path(config_version_id)
        if instrument and config_dir.exists():
            logger.debug("create_instance.overlay", source=str(config_dir))
            config_dir = OverlayBuilder.build(config_dir)
        handle = InstanceHandle(
            instance_id=instance_id,
            sut_id=image_ref,
            run_id=run_id,
            host=selected_host,
            container_id=instance_id,
            status="running",
            config_dir=config_dir,
            env={"RSI_LAB_ID": lab.lab_id, "RSI_RUN_ID": run_id},
        )
        self.net_policy.attach(selected_host, handle.container_id, [lab_net, trace_net])
        self.host_pool.increment(selected_host)
        self._instances[instance_id] = handle
        logger.debug("create_instance.exit", instance_id=instance_id, host=selected_host)
        return handle

    def submit_task(self, instance: InstanceHandle, task: TaskSpec) -> RunHandle:
        """Dispatch the task prompt to the instance's carrier adapter."""
        logger.debug(
            "submit_task.entry",
            instance_id=instance.instance_id,
            prompt_len=len(task.prompt),
            tool_constraints=len(task.tool_constraints),
        )
        adapter = self._adapter_for(instance.host)
        result = adapter.submit_task(instance, task)
        logger.debug("submit_task.exit", result_type=type(result).__name__)
        return result

    def collect(self, handle: RunHandle, timeout_s: float = 1800) -> TaskResult:
        """Collect stdout and extract the FINAL_JSON result."""
        logger.debug("collect.entry", instance_id=handle.instance_id, timeout_s=timeout_s)
        instance = self._instances.get(handle.instance_id)
        adapter = (
            self._adapter_for(instance.host) if instance else self._default_adapter
        )
        try:
            stdout = adapter.collect_result(handle, timeout_s)
        except Exception as exc:
            logger.debug("collect.instance_crashed", exc_type=type(exc).__name__)
            return TaskResult(task_complete=False, instance_crashed=True)
        result = extract_task_result(stdout)
        logger.debug(
            "collect.exit",
            task_complete=result.task_complete,
            parse_error=result.parse_error,
        )
        return result

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
