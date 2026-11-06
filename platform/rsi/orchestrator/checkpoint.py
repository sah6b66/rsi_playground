"""In-memory checkpoint persistence (P0) for batch resumption (DD-12 §4.4).

D-11: saves are throttled by ``orchestrator.checkpoint_interval_s`` — within
the interval only the latest state per scope is kept in a pending slot and
committed on the next flush; ``force=True`` (batch boundaries, aborts)
commits immediately. ``resume`` always sees the freshest state (pending
included), so throttling never loses resumption data.
"""

from __future__ import annotations

import time

from rsi.log import get_logger

logger = get_logger(__name__)


class CheckpointStore:
    """In-memory checkpoint persistence (P0) with interval-throttled writes.

    A later plan replaces the backing store with the ``checkpoints`` table;
    the async interface stays identical.
    """

    def __init__(self, interval_s: float | None = None) -> None:
        self.interval_s = float(interval_s) if interval_s else 0.0
        self._states: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        self._last_flush: dict[str, float] = {}
        self.flush_count = 0  # introspection/tests: committed writes

    async def save(self, scope: str, state: dict, *, force: bool = False) -> None:
        """Persist (upsert) the checkpoint state for a scope.

        Throttled: within ``interval_s`` of the previous flush the state only
        lands in the pending slot (merged into ``_states`` on the next flush,
        a forced save, or read).
        """
        logger.debug("save.entry", scope=scope, keys=sorted(state.keys()),
                      force=force)
        now = time.monotonic()
        due = force or (
            now - self._last_flush.get(scope, 0.0) >= self.interval_s
        )
        if due:
            self._commit(scope, state, now)
        else:
            self._pending[scope] = state
        logger.debug("save.exit", scope=scope, committed=due)

    def _commit(self, scope: str, state: dict, now: float) -> None:
        self._states[scope] = state
        self._pending.pop(scope, None)
        self._last_flush[scope] = now
        self.flush_count += 1

    async def resume(self, scope: str) -> dict | None:
        """Return the saved state for a scope, or ``None`` if absent."""
        logger.debug("resume.entry", scope=scope)
        state = self._pending.get(scope)
        if state is None:
            state = self._states.get(scope)
        else:
            # pending is fresher than the last committed flush
            self._commit(scope, state, time.monotonic())
        logger.debug("resume.exit", found=state is not None)
        return state
