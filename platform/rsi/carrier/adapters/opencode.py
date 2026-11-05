"""OpenCodeAdapter — P0 preferred SUT carrier (DD-05 §5.2, ADR-22; audit M2).

All docker/CLI interaction is deferred (asyncio.to_thread in the S11 real
backend phase); this implementation is fully offline — it only generates
config files (opencode.json) and constructs command/env records.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rsi.carrier.adapters.fake import CarrierAdapter, RunHandle, TraceChannels
from rsi.log import get_logger

if TYPE_CHECKING:  # runtime import would be circular (service imports adapters)
    from rsi.carrier.service import InstanceHandle

__all__ = ["OpenCodeAdapter"]

logger = get_logger(__name__)

IMAGE_BASE = "rsi/opencode-runtime:1.0"
IMAGE_REF = "rsi-opencode-runtime:1.0"
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"


class OpenCodeAdapter(CarrierAdapter):
    """P0 preferred carrier (ADR-22). Offline config/command generation only."""

    def __init__(self) -> None:
        # Per-container env (cred-derived DEV_* / OPENCODE_API_KEY), keyed by
        # container_id, for the service layer to read when spawning.
        self.env_for: dict[str, dict[str, str]] = {}

    def build_image(self, sut_id: str, config_dir: Any) -> str:
        logger.debug("opencode.build_image", sut_id=sut_id, image=IMAGE_REF)
        return IMAGE_REF

    def _read_model_config(self, config_dir: Any) -> tuple[str, str]:
        model_json = Path(config_dir) / "model.json"
        if model_json.exists():
            try:
                data = json.loads(model_json.read_text(encoding="utf-8"))
                return data.get("provider", DEFAULT_PROVIDER), data.get(
                    "model", DEFAULT_MODEL
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("opencode.model_json_unreadable", error=str(exc))
        return DEFAULT_PROVIDER, DEFAULT_MODEL

    def start(
        self, image_ref: str, config_dir: Any, permission: str, cred: Any
    ) -> str:
        """Write opencode.json into the mounted config dir; record env; offline."""
        provider, model = self._read_model_config(config_dir)
        # Wiretap file uses a session-level default in the shared
        # /agent-config mount; per-run attribution is closed out at
        # submit_task time via RunHandle.env["OPENCODE_WIRETAP_FILE"],
        # which the S11 real executor injects into the task process env.
        opencode_config = {
            "provider": provider,
            "model": model,
            "plugins": {
                "opencode-prompt-logger": {
                    "file": "/trace/wiretap/session.ndjson",
                    "rotateBytes": False,
                    "http": True,
                }
            },
        }
        out = Path(config_dir) / "opencode.json"
        out.write_text(json.dumps(opencode_config, indent=2), encoding="utf-8")
        container_id = f"oc-{uuid.uuid4()}"
        from rsi.carrier.service import build_dev_env

        self.env_for[container_id] = build_dev_env(cred)
        logger.debug(
            "opencode.start",
            container_id=container_id,
            image_ref=image_ref,
            permission=permission,
            env_keys=sorted(self.env_for[container_id]),
        )
        return container_id

    def submit_task(self, instance: "InstanceHandle", task: Any) -> RunHandle:
        prompt = task if isinstance(task, str) else task.prompt
        cmd = [
            "opencode",
            "run",
            "--format",
            "json",
            "--print-logs",
            "--session",
            instance.run_id,
            prompt,
        ]
        env: dict[str, str] = {
            # Per-run wiretap file so trace attribution matches
            # trace_channels (M2a); consumed by the S11 real executor.
            "OPENCODE_WIRETAP_FILE": f"/trace/wiretap/{instance.run_id}.ndjson"
        }
        container_id = getattr(instance, "container_id", None) or instance.instance_id
        env.update(self.env_for.get(container_id, {}))
        handle = RunHandle(
            run_id=instance.run_id,
            instance_id=instance.instance_id,
            cmd=cmd,
            env=env,
        )
        logger.debug("opencode.submit_task", run_id=instance.run_id)
        return handle

    def collect_result(self, handle: RunHandle, timeout_s: float) -> str:
        if handle.stdout is not None:
            return handle.stdout
        logger.warning(
            "opencode.collect_result.offline_stub",
            run_id=handle.run_id,
            timeout_s=timeout_s,
        )
        return ""

    def trace_channels(self, instance: "InstanceHandle") -> TraceChannels:
        return TraceChannels(
            kind="ndjson",
            paths=[
                f"/trace/wiretap/{instance.run_id}.ndjson",
                "/trace/events.ndjson",
            ],
        )
