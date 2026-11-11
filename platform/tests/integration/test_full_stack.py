"""F9 full-stack integration regressions on the production composition root.

- IN-015: labs table lifecycle (ready -> destroyed) through build_services.
- IN-016: trace event -> run linkage -> config version export reverse lookup.
- F1-F8 wiring regressions: judgments / llm_calls / judge_prompt_hash /
  config_versions rows land in the DB through the build_services chain, and
  the STOP circuit-breaker file round-trips through the API.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel import SQLModel, select

from rsi.api.app import create_app
from rsi.bootstrap import build_services
from rsi.carrier.adapters.fake import FakeCarrier
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    RootCauseExpectation,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.db.tables import (
    Batch,
    ConfigVersion,
    Judgment,
    Lab,
    LlmCall,
    Run,
)
from rsi.settings import DatabaseSettings, Settings
from rsi.tracing.store import TraceFilter

pytestmark = pytest.mark.asyncio

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
OUT = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown"}'


def _settings(tmp_path: Path) -> Settings:
    # C1: ``home`` is no longer a Settings init field — it is derived from
    # the RSI_HOME env var, so point the env at this test's tmp dir first.
    os.environ["RSI_HOME"] = str(tmp_path)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path/'fstack.db'}"),
    )


def _env(name: str = "lab-fstack") -> EnvSpec:
    return EnvSpec(
        name=name,
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
    )


def _task(n: int) -> TaskRecord:
    return TaskRecord(
        task_id=f"fault/fstack-{n:02d}",
        title=f"full-stack task {n}",
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
        ),
    )


def _services(tmp_path: Path) -> dict:
    services = build_services(_settings(tmp_path))
    SQLModel.metadata.create_all(services["engine"])
    # route the default adapter to a scripted FakeCarrier (offline)
    services["host_service"].adapters["opencode"] = FakeCarrier(
        responses={PROMPT: f"<FINAL_JSON>{OUT}</FINAL_JSON>"}
    )
    return services


async def _run_two_task_batch(services: dict, config_version_id: str = "cfg-f9"):
    return await services["batch_runner"].run_batch(
        "batch-f9", _env(), [_task(1), _task(2)], config_version_id, "sut-img",
        "read-only",
    )


# ---------------------------------------------------------------- IN-015


class TestIN015LabLifecycle:
    async def test_lab_rows_lifecycle_destroyed(self, tmp_path: Path) -> None:
        """批次结束后 labs 行存在且 status=destroyed（批次 finally destroy 落库）。"""
        services = _services(tmp_path)
        stats = await _run_two_task_batch(services)
        assert stats.n == 2

        with services["session_factory"]() as session:
            labs = session.exec(select(Lab)).all()
        assert labs, "labs row must be persisted via session_factory (F6 B-2)"
        assert all(lab.status == "destroyed" for lab in labs)
        assert all(lab.destroyed_at for lab in labs)
        # device endpoints (ADR-24 authority) recorded during build
        for lab in labs:
            assert "R1" in (lab.device_endpoints_json or "{}")

    async def test_manual_build_and_destroy_updates_row(self, tmp_path: Path) -> None:
        """直接经 SandboxService：build → status=ready；destroy → destroyed。"""
        services = _services(tmp_path)
        sandbox = services["sandbox_service"]
        lab = await sandbox.build_lab(_env("lab-manual"))
        with services["session_factory"]() as session:
            row = session.get(Lab, lab.lab_id)
        assert row is not None and row.status == "ready"
        await sandbox.destroy(lab)
        with services["session_factory"]() as session:
            row = session.get(Lab, lab.lab_id)
        assert row.status == "destroyed"


# ---------------------------------------------------------------- IN-016


class TestIN016TraceVersionLookup:
    async def test_trace_to_version_reverse_chain(self, tmp_path: Path) -> None:
        """轨迹事件(run_id) → 判决/llm_calls 记录(run_id 同源) → 版本内容导出。

        已知缺口（记录）：runs/batches 表无生产写入点，run→config_version
        的 DB 反查环缺失；测试用最近可用环（judgments/llm_calls.run_id）+
        GitStore 版本登记/导出，并显式断言该缺口。
        """
        services = _services(tmp_path)
        git = services["git_store"]
        git.init()
        git.write_file("sut-f9", "system-prompt.md", "# prompt v1\n")
        version_id = git.snapshot("sut-f9")

        stats = await _run_two_task_batch(services, config_version_id=str(version_id))
        assert stats.n == 2

        # ring 1: trace events per run, queryable via TraceStore
        with services["session_factory"]() as session:
            judgments = session.exec(select(Judgment)).all()
            llm_calls = session.exec(select(LlmCall)).all()
        assert len(judgments) == 2
        run_ids = {j.run_id for j in judgments}
        assert len(run_ids) == 2
        for run_id in run_ids:
            events, _ = services["trace_store"].query(
                TraceFilter(run_ids=[run_id]), cursor=None, limit=50
            )
            assert events, f"no trace events for judged run {run_id}"

        # ring 2: version registered in config_versions (DB) and exportable
        with services["session_factory"]() as session:
            cv = session.exec(
                select(ConfigVersion).where(ConfigVersion.id == str(version_id))
            ).all()
        assert cv, "snapshot must register a config_versions row"
        dest = tmp_path / "export"
        exported = git.export(version_id, dest)
        prompt_files = [
            p for p in exported.rglob("system-prompt.md") if p.is_file()
        ]
        assert prompt_files, "exported tree must contain the committed file"
        assert any("suts" in p.parts and "sut-f9" in p.parts for p in prompt_files)
        assert prompt_files[0].read_text(encoding="utf-8").startswith("# prompt v1")

        # F10: runs/batches rows now land through the production chain —
        # run -> batch_id -> batches.config_version_id reverse lookup works.
        with services["session_factory"]() as session:
            runs = session.exec(select(Run)).all()
            assert runs, "runs rows must be persisted by BatchRunner (F10)"
            assert len(runs) == 2
            run_ids_db = {r.id for r in runs}
            assert run_ids_db == run_ids, "run ids must match the judged runs"
            for run in runs:
                assert run.batch_id == "batch-f9"
                assert run.result_summary_json  # scores summary recorded
            batch = session.get(Batch, "batch-f9")
            assert batch is not None
            assert batch.status == "finalized"
            assert batch.finished_at
            assert batch.config_version_id == str(version_id)
            assert batch.purpose == "baseline_eval"  # adhoc -> baseline_eval


# ---------------------------------------------------------------- wiring regressions


class TestFullStackPersistence:
    async def test_judgments_llm_calls_prompt_hash(self, tmp_path: Path) -> None:
        services = _services(tmp_path)
        stats = await _run_two_task_batch(services)
        assert stats.n == 2

        with services["session_factory"]() as session:
            judgments = session.exec(select(Judgment)).all()
            llm_calls = session.exec(select(LlmCall)).all()
        # judgments: one per executed task via the session_factory chain
        assert len(judgments) >= 2
        # F7/C-1: the default build_services chain stamps a non-null prompt hash
        assert all(j.judge_prompt_hash for j in judgments)
        # C-7: Accountant persisted the LLM judge call(s)
        assert llm_calls, "llm_calls rows expected from the Accountant chain"
        assert all(call.component == "judge" for call in llm_calls)

    async def test_git_versions_register_rows(self, tmp_path: Path) -> None:
        """GitStore snapshot/commit_candidate 经 session_factory 登记
        config_versions 行（F5/GitStore DB 登记链）。"""
        services = _services(tmp_path)
        git = services["git_store"]
        git.init()
        # staging write then snapshot registers the version row. (Gap noted in
        # .f9-report.md: write_file(commit=True) alone registers nothing and a
        # following snapshot finds staging clean -> no DB row.)
        git.write_file("sut-cv", "system-prompt.md", "base\n")
        baseline = git.snapshot("sut-cv")
        with services["session_factory"]() as session:
            rows = session.exec(select(ConfigVersion)).all()
        assert any(r.id == str(baseline) for r in rows)
        assert any(r.kind == "baseline" for r in rows)


# ---------------------------------------------------------------- STOP chain (D-5)


class _TokenJudge:
    """Scripted judge reporting a fixed token usage per task."""

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.calls = 0

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        from rsi.core.models import (
            Efficiency,
            PathQuality,
            SafetyVerdict,
            Scores,
            Verdict,
        )

        self.calls += 1
        return Verdict(
            scores=Scores(
                root_cause_accuracy=0.9,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
            efficiency=Efficiency(tokens=self.tokens, tool_calls=1, wall_time_s=1.0),
            safety=SafetyVerdict(),
            missing=False,
        )


class TestStopFileChain:
    async def test_global_budget_stop_and_api_recovery(self, tmp_path: Path) -> None:
        services = _services(tmp_path)
        # script high per-task usage + a tight global limit so the tier-2
        # circuit breaker fires after the first task
        services["batch_runner"].judge = _TokenJudge(tokens=60_000)
        services["budget_controller"].global_limit = 50_000

        stats = await services["batch_runner"].run_batch(
            "batch-stop", _env("lab-stop"), [_task(1), _task(2)], "cfg-v1", "sut-img",
            "read-only",
        )
        assert services["batch_runner"].last_status == "aborted"
        assert stats.n == 1
        assert services["budget_controller"].check_stop_file() is True
        assert services["settings"].stop_file.exists()

        # API recovery (D-5): DELETE /system:stop removes the STOP file
        services["users"]["admin"] = {
            "user_id": "u-admin",
            "role": "admin",
            "password_hash": services["auth_service"].hash_password("admin-pw"),
        }
        app = create_app(services)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin-pw"},
            )
            h = {"Authorization": f"Bearer {login.json()['token']}"}
            resp = await client.delete("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
        assert resp.json()["stopped"] is False
        assert services["budget_controller"].check_stop_file() is False
        assert not services["settings"].stop_file.exists()
