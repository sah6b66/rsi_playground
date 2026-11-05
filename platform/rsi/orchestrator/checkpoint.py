"""In-memory checkpoint persistence (P0) for batch resumption (DD-12 §4.4)."""

from __future__ import annotations

from rsi.log import get_logger

logger = get_logger(__name__)


class CheckpointStore:
    """In-memory checkpoint persistence (P0).

    A later plan replaces the backing store with the ``checkpoints`` table;
    the async interface stays identical.
    """

    def __init__(self) -> None:
        self._states: dict[str, dict] = {}

    async def save(self, scope: str, state: dict) -> None:
        """Persist (upsert) the checkpoint state for a scope."""
        logger.debug("save.entry", scope=scope, keys=sorted(state.keys()))
        self._states[scope] = state
        logger.debug("save.exit", scope=scope)

    async def resume(self, scope: str) -> dict | None:
        """Return the saved state for a scope, or ``None`` if absent."""
        logger.debug("resume.entry", scope=scope)
        state = self._states.get(scope)
        logger.debug("resume.exit", found=state is not None)
        return state
