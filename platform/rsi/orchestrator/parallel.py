"""ParallelBatchRunner — max_parallel_batches consumer (B-3, DD-12 §4).

Runs multiple batches concurrently under an ``asyncio.Semaphore`` sized from
``settings.orchestrator.max_parallel_batches`` (default 2).

Concurrency-safety choice: BatchRunner keeps per-batch mutable state on the
instance (``_envs``, ``last_run_ids``, ``last_status``, the per-batch
instrument cache), while its shared state (services, checkpoints, the ADR-5
result cache) is either externally keyed by batch_id or content-hashed.
``run_many`` therefore gives each batch a shallow ``copy.copy`` of the
template runner: independent per-batch namespaces, while the result cache
dict, checkpoint store and services stay shared by reference.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from rsi.log import get_logger

from rsi.orchestrator.batch import BatchRunner

__all__ = ["ParallelBatchRunner"]

logger = get_logger(__name__)


class ParallelBatchRunner:
    def __init__(
        self,
        runner: BatchRunner,
        max_parallel: int | None = None,
        settings: Any | None = None,
    ) -> None:
        if max_parallel is None:
            max_parallel = (
                settings.orchestrator.max_parallel_batches
                if settings is not None
                else getattr(
                    getattr(runner, "settings", None),
                    "orchestrator",
                    None,
                ).max_parallel_batches
                if getattr(runner, "settings", None) is not None
                else 2
            )
        self.template = runner
        self.max_parallel = max(1, int(max_parallel))

    async def run_many(
        self, batches: list[dict[str, Any]]
    ) -> list[Any]:
        """Run ``run_batch(**kwargs)`` for each batch spec concurrently.

        Each batch gets its own shallow copy of the template runner
        (independent ``_envs`` / ``last_run_ids`` / instrument cache) while
        sharing the ADR-5 result cache, checkpoint store and services.
        """
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def _one(spec: dict[str, Any]):
            async with semaphore:
                runner = copy.copy(self.template)
                # F9 minimal fix (production bug found by
                # TestParallelBatchesIsolation): ``last_run_ids`` is keyed by
                # task_id (not batch-scoped), so a shallow copy would share
                # the dict and concurrent batches with overlapping task ids
                # would cross-write each other's run ids. Give each copy a
                # fresh map; the other per-batch state (_envs /
                # _instrument_cache) is already keyed by batch_id and the
                # shared result cache/checkpoint store are keyed externally.
                runner.last_run_ids = {}
                logger.debug(
                    "run_many.start", batch_id=spec.get("batch_id"),
                    max_parallel=self.max_parallel,
                )
                return await runner.run_batch(**spec)

        return list(await asyncio.gather(*(_one(spec) for spec in batches)))
