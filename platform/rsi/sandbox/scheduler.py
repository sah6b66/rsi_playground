"""LabScheduler (DD-04 §4.1 / ADR-24) — pick a backend instance for a lab.

Explicit ``backend_key`` always wins; otherwise the least-active backend of
the same kind as ``env.backend`` is selected so parallel batches spread
across sandbox instances. Candidates whose ``status().available`` is False
(e.g. prebuilt platform offline, T3) are skipped; if every candidate of the
requested kind is unavailable the selection fails with EnvError.
"""

from __future__ import annotations

from pydantic import BaseModel

from rsi.core.errors import EnvError
from rsi.log import get_logger
from rsi.core.models import EnvSpec
from rsi.sandbox.backends.base import SandboxBackend

__all__ = ["LabScheduler", "LabSelection"]

logger = get_logger(__name__)


class LabSelection(BaseModel):
    backend_key: str
    reason: str


class LabScheduler:
    def __init__(self, backends: dict[str, SandboxBackend]) -> None:
        self._backends = backends

    def select(self, env: EnvSpec, explicit_key: str | None = None) -> LabSelection:
        logger.debug("select.entry", env=env.name, explicit_key=explicit_key)
        if explicit_key is not None:
            if explicit_key not in self._backends:
                logger.debug("select.error", exc_type="EnvError", backend_key=explicit_key)
                raise EnvError(
                    f"explicit backend {explicit_key!r} not configured",
                    context={"backend_key": explicit_key, "env": env.name},
                )
            backend = self._backends[explicit_key]
            if not self._is_available(backend):
                logger.debug(
                    "select.error", exc_type="EnvError", backend_key=explicit_key,
                    reason="unavailable",
                )
                raise EnvError(
                    f"explicit backend {explicit_key!r} is unavailable",
                    context={"backend_key": explicit_key, "env": env.name},
                )
            logger.debug("select.exit", backend_key=explicit_key, reason="explicit")
            return LabSelection(
                backend_key=explicit_key,
                reason=f"explicit backend key {explicit_key!r}",
            )
        candidates = {
            k: b
            for k, b in self._backends.items()
            if b.kind == env.backend and self._is_available(b)
        }
        if not candidates:
            all_kind = [k for k, b in self._backends.items() if b.kind == env.backend]
            if all_kind:
                logger.debug(
                    "select.error", exc_type="EnvError", env_backend=env.backend,
                    reason="all_unavailable", backend_keys=sorted(all_kind),
                )
                raise EnvError(
                    f"all backends of kind {env.backend!r} unavailable",
                    context={
                        "env_backend": env.backend,
                        "env": env.name,
                        "backend_keys": sorted(all_kind),
                    },
                )
            logger.debug("select.error", exc_type="EnvError", env_backend=env.backend)
            raise EnvError(
                f"no backend of kind {env.backend!r} configured",
                context={"env_backend": env.backend, "env": env.name},
            )
        key = min(
            sorted(candidates), key=lambda k: candidates[k].active_lab_count
        )
        logger.debug(
            "select.exit", backend_key=key, strategy="least_active",
            active_labs=candidates[key].active_lab_count,
        )
        return LabSelection(
            backend_key=key,
            reason=(
                f"least-active {env.backend!r} backend "
                f"(active_labs={candidates[key].active_lab_count})"
            ),
        )

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _is_available(backend: SandboxBackend) -> bool:
        """Status probe must never break scheduling — an exception counts
        as unavailable rather than propagating into select()."""
        try:
            return backend.status().available
        except Exception:  # noqa: BLE001 - probe failure == offline
            return False
