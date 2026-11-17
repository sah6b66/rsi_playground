"""B2-T3：opt_history 生产装配——引擎缺参时经 opt_history_fn 取数（FR6.2）.

覆盖三条引擎路径（fn 取数 / 无 fn 默认 [] / 显式传参优先 + fn 异常降级）
以及 bootstrap 闭包（hypotheses 表该 SUT 近 20 条 -> 归因输入行）的映射。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from sqlmodel import Session as SqlSession

from rsi.core.models import AttributionReport, BatchStats
from rsi.orchestrator.iteration import IterationEngine

# -- 引擎桩 rig（以跑通为度：async analyze（B2-T2 形态）、async budget.record、
#    evalsets/goals 满足 DecisionCtx 校验）--------------------------------------


def _stats() -> BatchStats:
    return BatchStats(
        metrics={"task_complete": 1.0},
        n=1,
        missing=0,
        tokens=0,
        tool_calls=0,
        wall_time_s=0.0,
        tokens_estimated=False,
        task_scores=[],
    )


class _Runner:
    async def run_batch(self, name, env, tasks, cfg, img, *, db_meta=None):
        return _stats()


class _Bus:
    async def publish(self, *a, **k):
        return None


class _Budget:
    async def record(self, *a, **k):
        return None


class _Ana:
    def __init__(self, captured: list) -> None:
        self.captured = captured

    async def analyze(self, failed_runs, sut_config, opt_history):
        self.captured.append(list(opt_history or []))
        return AttributionReport(clusters=[])


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        name="t3-profile",
        enabled_targets=[],
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        guards={},
        evalsets=SimpleNamespace(smoke="", iterate="", holdout=""),
    )


def _engine(captured: list, opt_history_fn=None) -> IterationEngine:
    selector = SimpleNamespace(
        decide=lambda ctx: SimpleNamespace(
            action="HOLD", rule_hit=0, reason="stub", evidence={}
        )
    )
    return IterationEngine(
        _Runner(),
        _Ana(captured),
        selector,
        _Bus(),
        _Budget(),
        opt_history_fn=opt_history_fn,
    )


async def _run(eng: IterationEngine, **kw):
    return await eng.iterate_once(
        sut_id="sut-1",
        profile=_profile(),
        env=None,
        tasks=[],
        config_version_id="cfg",
        image_ref="img",
        **kw,
    )


# -- 引擎取数路径 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterate_uses_fn_when_param_none():
    captured: list = []
    eng = _engine(captured, lambda sut: [{"failure_mode": "m", "decision": "REJECTED"}])
    await _run(eng)
    assert captured and captured[0] == [{"failure_mode": "m", "decision": "REJECTED"}]


@pytest.mark.asyncio
async def test_no_fn_defaults_to_empty():
    """fn 为 None（既有桩 rig 形态）时行为不变：analyze 收到 []."""
    captured: list = []
    eng = _engine(captured, None)
    result = await _run(eng)
    assert eng.last_state == "FINISHED"
    assert captured and captured[0] == []
    assert result["decision"].action == "HOLD"


@pytest.mark.asyncio
async def test_explicit_opt_history_wins_over_fn():
    """调用方显式传入 opt_history 时优先生效，fn 不被调用."""
    captured: list = []
    fn_calls: list = []

    def _fn(sut):
        fn_calls.append(sut)
        return [{"from": "fn"}]

    eng = _engine(captured, _fn)
    await _run(eng, opt_history=[{"from": "caller"}])
    assert captured and captured[0] == [{"from": "caller"}]
    assert fn_calls == []


@pytest.mark.asyncio
async def test_fn_exception_degrades_to_empty():
    """fn 抛异常不阻断迭代：降级 [] 并正常走完状态机."""
    captured: list = []

    def _boom(sut):
        raise RuntimeError("db down")

    eng = _engine(captured, _boom)
    result = await _run(eng)
    assert eng.last_state == "FINISHED"
    assert captured and captured[0] == []
    assert result["decision"].action == "HOLD"


# -- bootstrap 闭包（hypotheses 表 -> 归因输入行）---------------------------------


def _db_factory(tmp_path):
    import rsi.db.tables  # noqa: F401 — 注册表模型

    engine = create_engine(f"sqlite:///{tmp_path}/opt_history.db")
    SQLModel.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=SqlSession)


def _hyp(sid: str, status: str, payload, created_at: str):
    from rsi.db.tables import Hypothesis

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return Hypothesis(
        id=f"hyp-{sid}-{status}-{created_at}",
        sut_id=sid,
        hypothesis_json=raw,
        status=status,
        created_at=created_at,
    )


class TestBootstrapOptHistoryClosure:
    def test_maps_recent_rows_newest_first(self, tmp_path):
        from rsi.bootstrap import _opt_history_lookup

        factory = _db_factory(tmp_path)
        with factory() as session:
            session.add(
                _hyp(
                    "sut-1",
                    "rejected",
                    {
                        "failure_type": "call_order_error",
                        "proposal_type": "modify_prompt",
                        "target_path": "suts/sut-1/sys.md",
                    },
                    "2026-01-01T00:00:00",
                )
            )
            session.add(
                _hyp("sut-1", "accepted", {"failure_type": "yang_path_error"},
                     "2026-01-02T00:00:00")
            )
            session.add(_hyp("sut-2", "applied", {"failure_type": "other"},
                             "2026-01-03T00:00:00"))
            session.commit()

        hist = _opt_history_lookup(factory, "sut-1")
        # 仅该 SUT、按 created_at 倒序
        assert [h["failure_mode"] for h in hist] == [
            "yang_path_error",
            "call_order_error",
        ]
        assert [h["decision"] for h in hist] == ["accepted", "rejected"]
        assert hist[1]["proposal_type"] == "modify_prompt"
        assert hist[1]["target"] == "suts/sut-1/sys.md"
        assert hist[0]["target"] is None
        assert hist[0]["proposal_type"] is None

    def test_limit_20_rows(self, tmp_path):
        from rsi.bootstrap import _opt_history_lookup

        factory = _db_factory(tmp_path)
        with factory() as session:
            for i in range(25):
                session.add(
                    _hyp("sut-1", "rejected", {"failure_type": f"m{i:02d}"},
                         f"2026-01-01T00:00:{i:02d}")
                )
            session.commit()
        hist = _opt_history_lookup(factory, "sut-1")
        assert len(hist) == 20
        # 倒序：最新的 20 条，最老的 5 条被截掉
        assert hist[0]["failure_mode"] == "m24"
        assert hist[-1]["failure_mode"] == "m05"

    def test_broken_json_row_degrades_to_empty_fields(self, tmp_path):
        from rsi.bootstrap import _opt_history_lookup

        factory = _db_factory(tmp_path)
        with factory() as session:
            session.add(_hyp("sut-1", "rejected", "not-json{", "2026-01-01T00:00:00"))
            session.commit()
        hist = _opt_history_lookup(factory, "sut-1")
        assert hist == [
            {"failure_mode": "", "decision": "rejected",
             "proposal_type": None, "target": None}
        ]

    def test_lookup_failure_degrades_to_empty(self):
        """session_factory 本身抛异常（无库/未 init）→ []，不阻断."""
        from rsi.bootstrap import _opt_history_lookup

        def _bad_factory():
            raise RuntimeError("no db")

        assert _opt_history_lookup(_bad_factory, "sut-1") == []
