"""In-memory checkpoint persistence (P0) for batch resumption (DD-12 §4.4)."""

from __future__ import annotations


class CheckpointStore:
    """In-memory checkpoint persistence (P0).

    A later plan replaces the backing store with the ``checkpoints`` table;
    the async interface stays identical.
    """

    def __init__(self) -> None:
        self._states: dict[str, dict] = {}

    async def save(self, scope: str, state: dict) -> None:
        """Persist (upsert) the checkpoint state for a scope."""
        self._states[scope] = state

    async def resume(self, scope: str) -> dict | None:
        """Return the saved state for a scope, or ``None`` if absent."""
        return self._states.get(scope)
