"""Three-tier budget controller (DD-12 §4.3): scope + global + STOP file."""

from __future__ import annotations

from pathlib import Path

from rsi.core.errors import BudgetExceeded
from rsi.log import get_logger

logger = get_logger(__name__)


class BudgetController:
    """Three-tier budget: scope (batch) + global + STOP file.

    Tier 1 (scope): ``check_and_reserve`` rejects a reservation when the
    scope's used tokens plus the estimate exceed the scope limit.
    Tier 2 (global): ``check_global`` sums usage across active (non-halted)
    scopes; on breach it creates the STOP file (global circuit breaker).
    Tier 3 (STOP file): ``check_stop_file`` reports whether the file exists.
    """

    def __init__(
        self,
        stop_file: Path,
        scope_limits: dict[str, int],
        global_limit: int,
    ) -> None:
        self.stop_file = Path(stop_file)
        self.scope_limits = dict(scope_limits)
        self.global_limit = global_limit
        self._used: dict[str, dict[str, float]] = {}
        self._halted: set[str] = set()

    async def check_and_reserve(self, scope: str, est_tokens: int) -> None:
        """Scope-level check: ``used + est > limit`` raises BudgetExceeded."""
        logger.debug("check_and_reserve.entry", scope=scope, est_tokens=est_tokens)
        if scope in self._halted:
            logger.debug("check_and_reserve.exceeded", scope=scope, reason="halted")
            raise BudgetExceeded(f"scope {scope!r} is halted")
        limit = self.scope_limits.get(scope)
        if limit is None:
            return
        used = self._used.get(scope, {}).get("tokens", 0)
        if used + est_tokens > limit:
            logger.debug(
                "check_and_reserve.exceeded", scope=scope, used=used, limit=limit
            )
            raise BudgetExceeded(
                f"scope budget exceeded for {scope!r}: "
                f"{used} + {est_tokens} > {limit} tokens"
            )
        logger.debug("check_and_reserve.exit", scope=scope, used=used, limit=limit)

    async def record(
        self, scope: str, tokens: int, cost_usd: float, wall_time_s: float
    ) -> None:
        """Track actual usage per scope."""
        logger.debug(
            "record.entry",
            scope=scope,
            tokens=tokens,
            cost_usd=cost_usd,
            wall_time_s=wall_time_s,
        )
        cur = self._used.setdefault(
            scope, {"tokens": 0, "cost_usd": 0.0, "wall_time_s": 0.0}
        )
        cur["tokens"] += tokens
        cur["cost_usd"] += cost_usd
        cur["wall_time_s"] += wall_time_s
        logger.debug("record.exit", scope=scope, total_tokens=cur["tokens"])

    async def check_global(self) -> None:
        """Global check: sum over active scopes; on breach create STOP file."""
        logger.debug("check_global.entry", global_limit=self.global_limit)
        total = sum(
            u["tokens"]
            for scope, u in self._used.items()
            if scope not in self._halted
        )
        if total > self.global_limit:
            logger.debug("check_global.exceeded", total=total)
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text(
                f"global budget exceeded: {total} > {self.global_limit} tokens\n",
                encoding="utf-8",
            )
            raise BudgetExceeded(
                f"global budget exceeded: {total} > {self.global_limit} tokens"
            )
        logger.debug("check_global.exit", total=total)

    async def halt(self, scope: str) -> None:
        """Mark a single batch/scope aborted without affecting others."""
        self._halted.add(scope)

    def check_stop_file(self) -> bool:
        """Return True if the STOP file exists."""
        return self.stop_file.exists()

    # -- introspection helpers ------------------------------------------------

    def usage(self, scope: str) -> dict[str, float]:
        """Accumulated usage for a scope (zeros when unrecorded)."""
        u = self._used.get(scope)
        if u is None:
            return {"tokens": 0, "cost_usd": 0.0, "wall_time_s": 0.0}
        return dict(u)

    def is_halted(self, scope: str) -> bool:
        return scope in self._halted
