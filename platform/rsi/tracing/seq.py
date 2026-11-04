"""SeqAllocator: per-run monotonic sequence numbers (DD-07 §4, NFR2-5).

Single in-process authority; file order == arrival order == seq order.
"""

from __future__ import annotations

__all__ = ["SeqAllocator"]


class SeqAllocator:
    """Allocate a strictly monotonic ``seq`` per ``run_id``, starting at 0."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, run_id: str) -> int:
        """Increment and return the counter for ``run_id``."""
        value = self._counters.get(run_id, 0)
        self._counters[run_id] = value + 1
        return value
