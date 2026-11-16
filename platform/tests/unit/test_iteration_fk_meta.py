"""B1-3：引擎 VERIFYING 批次 db_meta 携带真实 evalset_id（FK 完整性）.

根因：iteration.py VERIFYING 阶段 db_meta 只带 iteration_id/sut_id，
_db_insert_batch 空串兜底 → batches.evalset_id=""，join 断链。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from rsi.core.models import BatchStats, Decision, Goal, GuardPolicy
from rsi.orchestrator.iteration import IterationEngine


class _Bus:
    async def publish(self, *a, **k):
        return None


def _stats() -> BatchStats:
    return BatchStats(
        metrics={}, n=0, missing=0, tokens=0, tool_calls=0,
        wall_time_s=0.0, tokens_estimated=False, task_scores=[],
    )


class _Runner:
    """捕获 run_batch 的 db_meta；返回最小 BatchStats 形状。"""

    def __init__(self):
        self.metas: list[dict] = []

    async def run_batch(self, name, env, tasks, cfg, image_ref, *, db_meta=None):
        self.metas.append(dict(db_meta or {}))
        return _stats()


class _Budget:
    def remaining(self):
        return True

    def consume(self, *a):
        return None

    async def record(self, *a, **k):
        return None


def _engine(runner):
    return IterationEngine(
        runner,
        SimpleNamespace(analyze=lambda *a, **k: SimpleNamespace(
            clusters=[], directions=[], defect_counts={})),
        SimpleNamespace(decide=lambda ctx: Decision(
            action="ADVISE_MANUAL", rule_hit=0, reason="stub", evidence={})),
        _Bus(),
        _Budget(),
    )


def _profile():
    return SimpleNamespace(
        name="stub",
        enabled_targets=[],
        goals=[Goal(metric="task_success_rate", direction="max", weight=1.0)],
        guards=GuardPolicy(),
        evalsets=SimpleNamespace(smoke="", iterate="", holdout="h-1"),
    )


@pytest.mark.asyncio
async def test_iterate_body_carries_evalset_id_in_db_meta():
    runner = _Runner()
    eng = _engine(runner)
    await eng._iterate_body(
        "iter-1", None, "sut-1",
        _profile(),
        None, [], "cfg-1", "img:1", None,
        failed_runs=[], sut_config={}, opt_history=[],
        hypothesis=None, candidate_config_version_id=None,
        noise_delta=None,
        evalset_id="es-1",
    )
    assert runner.metas, "engine should have triggered at least one batch"
    for m in runner.metas:
        assert m.get("evalset_id") == "es-1"
        assert m.get("sut_id") == "sut-1"
        assert m.get("iteration_id") == "iter-1"


@pytest.mark.asyncio
async def test_iterate_body_defaults_evalset_id_empty():
    """向后兼容：不传 evalset_id 时 db_meta 仍含键（空串），不破坏既有调用。"""
    runner = _Runner()
    eng = _engine(runner)
    await eng._iterate_body(
        "iter-2", None, "sut-2",
        _profile(),
        None, [], "cfg-2", "img:1", None,
        failed_runs=[], sut_config={}, opt_history=[],
        hypothesis=None, candidate_config_version_id=None,
        noise_delta=None,
    )
    assert runner.metas
    for m in runner.metas:
        assert m.get("evalset_id") == ""
