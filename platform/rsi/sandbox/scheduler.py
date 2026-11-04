"""LabScheduler (DD-04 §4.1 / ADR-24) — pick a backend instance for a lab.

Explicit ``backend_key`` always wins; otherwise the least-active backend of
the same kind as ``env.backend`` is selected so parallel batches spread
across sandbox instances.
"""

from __future__ import annotations

from pydantic import BaseModel

from rsi.core.errors import EnvError
from rsi.core.models import EnvSpec
from rsi.sandbox.backends.base import SandboxBackend

__all__ = ["LabScheduler", "LabSelection"]


class LabSelection(BaseModel):
    backend_key: str
    reason: str


class LabScheduler:
    def __init__(self, backends: dict[str, SandboxBackend]) -> None:
        self._backends = backends

    def select(self, env: EnvSpec, explicit_key: str | None = None) -> LabSelection:
        if explicit_key is not None:
            if explicit_key not in self._backends:
                raise EnvError(
                    f"explicit backend {explicit_key!r} not configured",
                    context={"backend_key": explicit_key, "env": env.name},
                )
            return LabSelection(
                backend_key=explicit_key,
                reason=f"explicit backend key {explicit_key!r}",
            )
        candidates = {
            k: b for k, b in self._backends.items() if b.kind == env.backend
        }
        if not candidates:
            raise EnvError(
                f"no backend of kind {env.backend!r} configured",
                context={"env_backend": env.backend, "env": env.name},
            )
        key = min(
            sorted(candidates), key=lambda k: candidates[k].active_lab_count
        )
        return LabSelection(
            backend_key=key,
            reason=(
                f"least-active {env.backend!r} backend "
                f"(active_labs={candidates[key].active_lab_count})"
            ),
        )
