"""Accountant — per-component LLM usage tracking for budget control (DD-02 §4).

In-memory usage ledger: every recorded call appends a flat record; aggregate
views (``sum_usage``) feed budget checks and cost dashboards.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["Accountant"]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Accountant:
    """Tracks LLM usage per component for budget control."""

    def __init__(self):
        self._records: list[dict] = []

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
                "ts": _utcnow_iso(),
            }
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
