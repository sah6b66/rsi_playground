"""DB-backed read fallback for run/judgment endpoints (audit4 W-C).

Production ``BatchRunner`` persists runs only to the ``runs`` table — the
in-memory ``runs`` store is populated exclusively by test rigs. Read
endpoints therefore consult the in-memory store first (test-injected data
stays visible) and fall back to a SELECT through ``session_factory`` when
the memory store misses and the DI container registers a DB session
factory. Response models are unchanged: DB rows are reshaped into the same
dict form the memory store uses.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request


def _session_factory(request: Request):
    return request.app.state.services.get("session_factory")


def _row_to_run(row: Any) -> dict:
    result = None
    if row.result_summary_json:
        try:
            result = json.loads(row.result_summary_json)
        except (TypeError, ValueError):
            result = None
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "task_id": row.eval_task_id,
        "status": row.status,
        "result": result,
    }


def get_run_record(request: Request, run_id: str, store: dict) -> dict | None:
    """Memory-first run lookup with a DB fallback; ``None`` when absent."""
    if run_id in store:
        return store[run_id]
    factory = _session_factory(request)
    if factory is None:
        return None
    try:
        from rsi.db.tables import Run

        with factory() as session:  # type: ignore[misc]
            row = session.get(Run, run_id)
            return _row_to_run(row) if row is not None else None
    except Exception:  # noqa: BLE001 - read fallback must never 500
        return None


def list_runs_for_batch(
    request: Request, store: dict, batch_id: str
) -> list[dict]:
    """All runs of a batch: memory hits plus DB rows, deduped by run id.

    Order follows the DB ``seq_in_batch`` when available (memory-only rigs
    keep insertion order, which is also batch order).
    """
    out: dict[str, dict] = {
        r["id"]: r
        for r in store.values()
        if r.get("batch_id") == batch_id
    }
    factory = _session_factory(request)
    if factory is not None:
        try:
            from sqlmodel import select

            from rsi.db.tables import Run

            with factory() as session:  # type: ignore[misc]
                rows = session.exec(
                    select(Run)
                    .where(Run.batch_id == batch_id)
                    .order_by(Run.seq_in_batch)
                ).all()
                for row in rows:
                    out.setdefault(row.id, _row_to_run(row))
        except Exception:  # noqa: BLE001 - read fallback must never 500
            pass
    return list(out.values())


def db_verdict(request: Request, run_id: str) -> dict | None:
    """Parse the latest ``judgments`` row of a run into verdict-dict form.

    Returns ``{verdict, score, reasons, dimensions}`` or ``None`` when the
    DB is unavailable / has no judgment for the run. Used by the judgment
    endpoints when the run record came from the DB (no memory verdict).
    """
    factory = _session_factory(request)
    if factory is None:
        return None
    try:
        from sqlmodel import select

        from rsi.db.tables import Judgment

        with factory() as session:  # type: ignore[misc]
            row = session.exec(
                select(Judgment).where(Judgment.run_id == run_id)
            ).first()
            if row is None or not row.verdict_json:
                return None
            try:
                data = json.loads(row.verdict_json)
            except (TypeError, ValueError):
                return None
            if not isinstance(data, dict):
                return None
            return {
                "verdict": data.get("verdict", "pending"),
                "score": data.get("score", 0.0),
                "reasons": list(data.get("reasons", [])),
                "dimensions": dict(data.get("dimensions", {})),
            }
    except Exception:  # noqa: BLE001 - read fallback must never 500
        return None


__all__ = ["db_verdict", "get_run_record", "list_runs_for_batch"]
