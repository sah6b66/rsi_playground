"""CarrierAdapter ABC + offline FakeCarrier (DD-05, P0)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # runtime import would be circular (service imports adapters)
    from rsi.carrier.service import InstanceHandle


class TraceChannels(BaseModel):
    """Where a carrier emits its trace streams."""

    kind: str  # e.g. "ndjson"
    paths: list[str]


class RunHandle(BaseModel):
    """In-flight SUT task execution reference."""

    run_id: str
    instance_id: str
    cmd: list[str]
    stdout: str | None = None
    # Container/agent process exit code (B-10/G5): filled by the S11 real
    # executor; 137/139 classify the run as instance-crashed in collect.
    exit_code: int | None = None
    # Extra env for the S11 real executor to inject when spawning the task
    # (e.g. OPENCODE_WIRETAP_FILE for per-run trace attribution).
    env: dict[str, str] = Field(default_factory=dict)


class CarrierAdapter(ABC):
    """SUT carrier interface (opencode / claude_code / fake)."""

    @abstractmethod
    def build_image(self, sut_id: str, config_dir: Any) -> str:
        """Build/prepare the SUT image; returns image reference."""

    @abstractmethod
    def start(
        self, image_ref: str, config_dir: Any, permission: str, cred: Any
    ) -> str:
        """Start a SUT container; returns container_id."""

    @abstractmethod
    def submit_task(self, instance: "InstanceHandle", task: Any) -> RunHandle:
        """Submit a task prompt to a running instance."""

    @abstractmethod
    def collect_result(self, handle: RunHandle, timeout_s: float) -> str:
        """Block until the task finishes; returns raw stdout."""

    @abstractmethod
    def trace_channels(self, instance: "InstanceHandle") -> TraceChannels:
        """Locate the carrier's trace output streams for an instance."""


class FakeCarrier(CarrierAdapter):
    """Offline scripted carrier: task prompt -> canned stdout."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses: dict[str, str] = responses or {}

    def build_image(self, sut_id: str, config_dir: Any) -> str:
        return f"fake-image:{sut_id}"

    def start(
        self, image_ref: str, config_dir: Any, permission: str, cred: Any
    ) -> str:
        return f"fake-container:{image_ref}"

    def submit_task(self, instance: "InstanceHandle", task: Any) -> RunHandle:
        prompt = task.prompt
        return RunHandle(
            run_id=instance.run_id,
            instance_id=instance.instance_id,
            cmd=["fake-carrier", "run", prompt],
        )

    def collect_result(self, handle: RunHandle, timeout_s: float) -> str:
        prompt = handle.cmd[-1] if handle.cmd else ""
        return self.responses.get(prompt, "")

    def trace_channels(self, instance: "InstanceHandle") -> TraceChannels:
        return TraceChannels(
            kind="ndjson",
            paths=[f"fake-trace/{instance.instance_id}/events.ndjson"],
        )
