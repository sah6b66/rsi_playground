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
        estimated: bool = False,
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
                "estimated": estimated,
                "ts": ts,
            }
        )
        self._persist(
            ts, component, model, prompt_version, input_tokens, output_tokens,
            latency_ms, cost_usd, run_id, batch_id, estimated,
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
        estimated: bool = False,
    ) -> None:
        """Insert one llm_calls row; failures degrade to a warning (C-7).

        B2-T8：SQLModel create_all 不迁移既有库——旧库缺 ``estimated`` 列时
        捕 SQLite 缺列 OperationalError → ``ALTER TABLE`` 补列后重试一次
        （幂等：列已存在则插入直接成功，永不触发 ALTER）。
        """
        if self._session_factory is None:
            return
        row_kwargs: dict = {
            "ts": ts,
            "component": component,
            "model": model,
            "prompt_version": prompt_version,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "run_id": run_id,
            "batch_id": batch_id,
            "estimated": estimated,
        }
        try:
            # B2 终审 Minor#7：import 在 try 圈内——db.tables 导入失败
            # （如缺 DB 依赖）同样降级 warning，不炸调用路径（C-7 铁律）
            from rsi.db.tables import LlmCall

            with self._session_factory() as session:
                session.add(LlmCall(**row_kwargs))
                session.commit()
        except Exception as exc:  # noqa: BLE001 - accounting must not break calls
            if self._try_alter_estimated_column(exc, component, row_kwargs):
                return
            logger.warning(
                "accountant.persist_failed",
                component=component,
                exc_type=type(exc).__name__,
            )

    def _try_alter_estimated_column(
        self, exc: Exception, component: str, row_kwargs: dict
    ) -> bool:
        """旧库缺列自迁移：命中缺 estimated 列的 OperationalError → ALTER + 重试一次.

        SQLite 两种措辞都认：INSERT 路径 "has no column named estimated"、
        SELECT/UPDATE 路径 "no such column: estimated"。只对缺列生效——
        列已存在时插入直接成功，永不触发 ALTER（幂等）。

        Returns True when the row was persisted after the ALTER.
        """
        msg = str(exc).lower()
        if "estimated" not in msg or not (
            "no such column" in msg or "has no column named" in msg
        ):
            return False
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                conn = session.connection()
                conn.exec_driver_sql(
                    "ALTER TABLE llm_calls ADD COLUMN estimated BOOLEAN DEFAULT 0"
                )
                session.commit()
            from rsi.db.tables import LlmCall

            with self._session_factory() as session:  # type: ignore[misc]
                session.add(LlmCall(**row_kwargs))
                session.commit()
            logger.info("accountant.alter_estimated_column_done")
            return True
        except Exception as alter_exc:  # noqa: BLE001 - ALTER 失败同样只降级告警
            logger.warning(
                "accountant.alter_estimated_column_failed",
                component=component,
                exc_type=type(alter_exc).__name__,
            )
            return False

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
