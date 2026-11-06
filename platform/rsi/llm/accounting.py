"""Accountant — per-component LLM usage tracking for budget control (DD-02 §4/§6).

Usage ledger: every recorded call appends a flat in-memory record and, when a
``session_factory`` is wired, also inserts one row into the ``llm_calls``
table (DD-02 §6). DB failures are logged as warnings and never break the
call path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable, TYPE_CHECKING

from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from sqlalchemy.orm import Session

__all__ = ["Accountant"]

logger = get_logger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Accountant:
    """Tracks LLM usage per component for budget control."""

    def __init__(self, session_factory: 'Callable[["Session"]] | None' = None):
        self._records: list[dict] = []
        self._session_factory = session_factory

    def record(
        self,
        component: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        cost_usd: float | None = None,
        run_id: str | None = None,
        batch_id: str | None = None,
    ) -> None:
        ts = _utcnow_iso()
        self._records.append(
            {
                "component": component,
                "model": model,
                "prompt_version": prompt_version,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
                "run_id": run_id,
                "batch_id": batch_id,
                "ts": ts,
            }
        )
        self._persist(
            ts, component, model, prompt_version, input_tokens, output_tokens,
            latency_ms, cost_usd, run_id, batch_id,
        )

    def _persist(
        self,
        ts: str,
        component: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        cost_usd: float | None,
        run_id: str | None,
        batch_id: str | None,
    ) -> None:
        """Insert one llm_calls row; failures degrade to a warning (C-7)."""
        if self._session_factory is None:
            return
        try:
            from rsi.db.tables import LlmCall

            row = LlmCall(
                ts=ts,
                component=component,
                model=model,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                run_id=run_id,
                batch_id=batch_id,
            )
            with self._session_factory() as session:
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - accounting must not break calls
            logger.warning(
                "accountant.persist_failed",
                component=component,
                exc_type=type(exc).__name__,
            )

    def sum_usage(self, scope: str | None = None) -> dict:
        """Sum tokens/cost/latency, optionally filtered by run_id or batch_id."""
        rows = [
            r
            for r in self._records
            if scope is None or r["run_id"] == scope or r["batch_id"] == scope
        ]
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        return {
            "calls": len(rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "latency_ms": sum(r["latency_ms"] for r in rows),
            "cost_usd": sum(costs) if costs else None,
        }

    def records(self) -> list[dict]:
        return list(self._records)
