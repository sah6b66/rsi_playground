"""Merge-blocker fixes (final review round): BatchRunner.trigger(), path
traversal, per-call bypass_cache, DecisionCtx wiring, freeze consumer,
cassette prompt fingerprint + quality items."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.core.errors import GitStoreError, TargetViolationError
from rsi.core.models import (
    BatchStats,
    Decision,
    EvalsetBinding,
    Goal,
    OptimizationProfile,
)
from rsi.decision.recorder import DecisionRecorder
from rsi.judge.rules import freeze_check
from rsi.llm.client import LLMClient
from rsi.llm.prompts import PromptLibrary
from rsi.optimizer.targets import TargetRegistry
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.iteration import RUNNING_ENGINES, IterationEngine
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.sandbox.service import normalize_config
from rsi.versioning.gitstore import GitStore

pytestmark = pytest.mark.asyncio


# ── 2. path traversal ────────────────────────────────────────────────────


class TestPathTraversal:
    def test_registry_rejects_dotdot(self):
        reg = TargetRegistry()
        with pytest.raises(TargetViolationError):
            reg.validate(
                "suts/sut-1/skills/../../../../../tmp/pwn", {"skills"}
            )
        with pytest.raises(TargetViolationError):
            reg.validate("suts/sut-1/../../etc/passwd", {"skills"})
        with pytest.raises(TargetViolationError):
            reg.validate("/etc/passwd", {"skills"})
        with pytest.raises(TargetViolationError):
            reg.validate("suts/sut-1/skills\\..\\..\\evil", {"skills"})
        # clean path still passes
        reg.validate("suts/sut-1/skills/bgp.md", {"skills"})

    def test_gitstore_write_delete_reject_traversal(self, tmp_path: Path):
        gs = GitStore(
            repo_path=tmp_path / "repo.git",
            worktree_root=tmp_path / "wts",
        )
        gs.init()
        with pytest.raises(GitStoreError):
            gs.write_file("sut-1", "../../evil", "x")
        with pytest.raises(GitStoreError):
            gs.delete_file("sut-1", "../evil")
        # nothing escaped the staging tree
        assert not (tmp_path / "evil").exists()
        staging = gs.worktrees.staging()
        outside = [
            p for p in tmp_path.rglob("*") if ".." in p.name and p.is_file()
        ]
        assert outside == [] or all(staging not in p.parents for p in outside)

    def test_gitstore_delete_commit_registers_version(self, tmp_path: Path):
        gs = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wts"
        )
        gs.init()
        gs.write_file("sut-1", "system-prompt.md", "hello")
        registered: list[tuple] = []
        gs._register_version = lambda *a, **k: registered.append(a)  # type: ignore
        gs.delete_file("sut-1", "system-prompt.md", commit=True)
        assert registered and registered[0][1] == "sut-1"


# ── 1 + 3. BatchRunner.trigger + per-call bypass ─────────────────────────


def _bare_runner() -> BatchRunner:
    return BatchRunner(
        sandbox=None, carrier=None, collector=None, trace_store=None,
        judge=None, ckpt=None, budget=None, event_bus=None, stats_agg=None,
    )


class TestTrigger:
    async def test_trigger_runs_run_batch_in_background(self):
        runner = _bare_runner()
        calls: list[tuple[tuple, dict]] = []

        async def fake_run_batch(*args, **kwargs):
            calls.append((args, kwargs))
            return "stats"

        runner.run_batch = fake_run_batch  # type: ignore
        batch = {"id": "batch-x", "evalset_id": "es-1", "sut_id": "sut-1"}
        runner.trigger(batch)
        await asyncio.wait_for(asyncio.gather(*runner._tasks), timeout=5)
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "batch-x"
        assert kwargs["db_meta"]["evalset_id"] == "es-1"
        assert kwargs["bypass_cache"] is False  # runner default, not sticky

    async def test_trigger_per_call_bypass_cache(self):
        runner = _bare_runner()
        seen: list[bool | None] = []

        async def fake_run_batch(*args, **kwargs):
            seen.append(kwargs.get("bypass_cache"))

        runner.run_batch = fake_run_batch  # type: ignore
        runner.trigger({"id": "b1"}, bypass_cache=True)
        runner.trigger({"id": "b2"})  # ordinary batch afterwards
        await asyncio.wait_for(asyncio.gather(*runner._tasks), timeout=5)
        assert seen == [True, False]
        # the sticky runner-level default was never flipped
        assert runner.bypass_cache is False

    async def test_trigger_params_bypass_flag(self):
        runner = _bare_runner()
        seen: list[bool | None] = []

        async def fake_run_batch(*args, **kwargs):
            seen.append(kwargs.get("bypass_cache"))

        runner.run_batch = fake_run_batch  # type: ignore
        runner.trigger({"id": "b1", "params": {"bypass_cache": True}})
        await asyncio.wait_for(asyncio.gather(*runner._tasks), timeout=5)
        assert seen == [True]

    async def test_trigger_contains_failures(self):
        runner = _bare_runner()

        async def boom(*args, **kwargs):
            raise RuntimeError("sandbox exploded")

        runner.run_batch = boom  # type: ignore
        # audit3 W3: trigger runs on an isolated per-batch copy (returned)
        r = runner.trigger({"id": "b-bad"})  # must not raise
        await asyncio.wait_for(asyncio.gather(*r._tasks), timeout=5)
        assert r.last_status == "failed"

    async def test_trigger_from_sync_context(self):
        runner = _bare_runner()
        calls: list[str] = []

        async def fake_run_batch(*args, **kwargs):
            calls.append(args[0])

        runner.run_batch = fake_run_batch  # type: ignore
        # call trigger() with no running loop (sync context -> thread)
        await asyncio.to_thread(runner.trigger, {"id": "b-thread"})
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert calls == ["b-thread"]

    async def test_default_assembly_post_batches_accepted(self, tmp_path, monkeypatch):
        """create_app() default assembly (real bootstrap): POST /batches
        must not 500 — the batch is accepted (201) and the triggered
        background batch runs through the offline fake rig."""
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        from rsi.bootstrap import build_services

        services = build_services()
        auth: AuthService = services["auth_service"]
        pw_hash = auth.hash_password("pw")
        token, _claims = auth.login("u-admin", "admin", pw_hash, "pw")
        app = create_app(services)
        runner: BatchRunner = services["batch_runner"]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # seed the in-memory stores (git store is not initialized in the
            # offline assembly, so create the records directly)
            app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
            app.state.stores["evalsets"]["es-1"] = {"id": "es-1"}
            resp = await client.post(
                "/api/v1/batches",
                json={"evalset_id": "es-1", "sut_id": "sut-1", "purpose": "iterate"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 201, resp.text
            batch_id = resp.json()["id"]
            # the background batch runs (or is contained) without error
            if runner._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*runner._tasks, return_exceptions=True),
                    timeout=30,
                )
            got = await client.get(
                f"/api/v1/batches/{batch_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert got.status_code == 200
            # audit3 W2: the in-memory store is synced by the completion
            # callback — the batch reaches a terminal status, not "created".
            assert got.json()["status"] != "created"


class _RerunRunner:
    """Fake runner: records per-call bypass, exposes a sticky attr."""

    def __init__(self):
        self.calls: list[tuple[str, bool | None]] = []
        self.bypass_cache = False

    def trigger(self, batch: dict, *, bypass_cache: bool | None = None):
        self.calls.append((batch["id"], bypass_cache))


def _api_services(runner) -> dict:
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "batch_runner": runner,
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("pw"),
            }
        },
    }


class TestRerunBypass:
    async def test_rerun_passes_bypass_per_call(self, tmp_path):
        runner = _RerunRunner()
        app = create_app(_api_services(runner))
        app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
        app.state.stores["evalsets"]["es-1"] = {"id": "es-1"}
        app.state.stores["batches"]["b-old"] = {
            "id": "b-old",
            "evalset_id": "es-1",
            "sut_id": "sut-1",
            "purpose": "iterate",
            "profile_id": None,
            "status": "failed",
            "paused": False,
            "progress": {"done": 0, "total": 0},
            "params": {},
            "created_at": "2026-01-01T00:00:00",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = await _login(client)
            resp = await client.post(
                "/api/v1/batches/b-old:rerun",
                json={"scope": "failed", "bypass_cache": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 204, resp.text
        assert runner.calls and runner.calls[0][1] is True
        # sticky default untouched: ordinary batches still hit the cache
        assert runner.bypass_cache is False


async def _login(client) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "pw"}
    )
    return resp.json()["token"]


# ── 4. DecisionCtx wiring + recorder robustness ──────────────────────────


def _stats(prefix: str, scores: list[float], ids: list[str] | None = None) -> BatchStats:
    return BatchStats(
        metrics={"task_complete": sum(scores) / len(scores)},
        n=len(scores),
        missing=0,
        tokens=100,
        tool_calls=3,
        wall_time_s=10.0,
        tokens_estimated=False,
        task_scores=[
            {"task_id": (ids[i] if ids else f"fault/{prefix}-{i:02d}"), "score": s}
            for i, s in enumerate(scores)
        ],
    )


class _Bus:
    async def publish(self, *a, **k):
        return None


class _Budget:
    async def record(self, *a, **k):
        return None

    async def check_global(self):
        return None


class _Attribution:
    def analyze(self, *a, **k):
        class R:
            clusters: list = []
        return R()


class _Selector:
    def __init__(self):
        self.ctx = None

    def decide(self, ctx):
        self.ctx = ctx
        return Decision(action="PROMOTE", rule_hit=1, reason="t", evidence={})


class _Recorder(DecisionRecorder):
    def __init__(self):
        super().__init__()
        self.kwargs: dict = {}

    def record(self, exp_id, decision, base_id, cand_id, **kwargs):
        self.kwargs = kwargs
        return super().record(exp_id, decision, base_id, cand_id, **kwargs)


class _BatchStub:
    def __init__(self, base: BatchStats, cand: BatchStats):
        self.base, self.cand = base, cand

    async def run_batch(self, batch_id, *a, **k):
        return self.cand if "holdout" in batch_id else self.base


def _profile() -> OptimizationProfile:
    return OptimizationProfile(
        name="p",
        evalsets=EvalsetBinding(iterate="es-it", holdout="es-holdout", smoke=""),
        goals=[Goal(metric="task_complete", direction="max", weight=1.0)],
        enabled_targets={"system_prompt"},
    )


class TestDecisionCtxWiring:
    async def test_ctx_gets_noise_scores_cost_and_recorder_ids(self):
        selector = _Selector()
        recorder = _Recorder()
        ids = ["fault/t-00", "fault/t-01"]
        base = _stats("a", [0.5, 0.7], ids)
        cand = _stats("b", [0.8, 0.9], ids)
        engine = IterationEngine(
            batch_runner=_BatchStub(base, cand),
            attribution_analyzer=_Attribution(),
            selector=selector,
            event_bus=_Bus(),
            budget=_Budget(),
            decision_recorder=recorder,
            noise_lookup=lambda es: {"task_complete": 0.1}
            if es == "es-holdout" else {},
        )
        result = await engine.iterate_once(
            "sut-9",
            _profile(),
            env=object(),
            tasks=[],
            config_version_id="cfg-base",
            image_ref="img",
            baseline_stats=base,
            candidate_config_version_id="cfg-cand",
        )
        ctx = selector.ctx
        assert ctx is not None
        assert ctx.metric_noise == {"task_complete": 0.1}
        assert ctx.base_task_scores == [0.5, 0.7]
        assert ctx.cand_task_scores == [0.8, 0.9]
        assert ctx.cost["tokens"] == base.tokens + cand.tokens
        assert ctx.cost["wall_time_s"] == base.wall_time_s + cand.wall_time_s
        assert ctx.cost["tool_calls"] == base.tool_calls + cand.tool_calls
        # recorder now receives the real identities
        assert recorder.kwargs["sut_id"] == "sut-9"
        assert recorder.kwargs["baseline_version_id"] == "cfg-base"
        assert recorder.kwargs["candidate_version_id"] == "cfg-cand"
        assert result["decision"].action == "PROMOTE"

    async def test_no_noise_lookup_leaves_metric_noise_none(self):
        selector = _Selector()
        ids = ["fault/t-00"]
        base, cand = _stats("a", [0.5], ids), _stats("b", [0.6], ids)
        engine = IterationEngine(
            batch_runner=_BatchStub(base, cand),
            attribution_analyzer=_Attribution(),
            selector=selector,
            event_bus=_Bus(),
            budget=_Budget(),
        )
        await engine.iterate_once(
            "sut-9", _profile(), env=object(), tasks=[],
            config_version_id="cfg", image_ref="img", baseline_stats=base,
            candidate_config_version_id="cand",
        )
        assert selector.ctx.metric_noise is None
        assert selector.ctx.base_task_scores is not None

    def test_recorder_db_failure_is_contained(self):
        def broken_factory():
            raise RuntimeError("db down")

        recorder = DecisionRecorder(session_factory=broken_factory)
        recorder.record(  # must not raise
            "exp-x",
            Decision(action="REVERT", rule_hit=0, reason="t", evidence={}),
            "b-1", "b-2",
        )
        assert len(recorder.get("exp-x")) == 1


# ── 5. engine bridge id space ────────────────────────────────────────────


class TestEngineBridge:
    async def test_gate_endpoint_reaches_engine_registered_under_api_id(self):
        engine = IterationEngine(
            batch_runner=None,
            attribution_analyzer=_Attribution(),
            selector=_Selector(),
            event_bus=_Bus(),
            budget=_Budget(),
        )
        # simulate an engine paused at gate 1 under the API-minted id
        engine.last_state = "PAUSED"
        engine.current_step = "awaiting_direction"
        engine._pending_gate = "confirm_direction"
        api_id = "iter-deadbeef"
        RUNNING_ENGINES[api_id] = engine
        try:
            app = create_app({"auth_service": AuthService(secret="s"),
                              "audit_log": AuditLog(), "users": {}})
            app.state.stores["iterations"][api_id] = {
                "id": api_id, "sut_id": "sut-1", "profile_id": "p-1",
                "mode": "p1", "state": "paused",
                "current_step": "awaiting_direction", "round": 1,
                "experiments": [], "budget_used": {}, "archived": False,
                "paused_from": "ATTRIBUTING", "gates": [],
                "created_at": "2026-01-01T00:00:00",
            }
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # admin user missing -> seed one
                auth = app.state.services["auth_service"]
                app.state.services["users"]["admin"] = {
                    "user_id": "u-admin", "role": "admin",
                    "password_hash": auth.hash_password("pw"),
                }
                token = await _login(client)
                resp = await client.post(
                    f"/api/v1/iterations/{api_id}/confirm-direction",
                    json={"selected": "exp-1"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 204, resp.text
            # the live engine's gate event was actually fired
            assert engine._gate_events["confirm_direction"].is_set()
        finally:
            RUNNING_ENGINES.pop(api_id, None)


# ── 6. freeze verify + doctor consumer ───────────────────────────────────


class TestFreeze:
    def _make_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "judge"
        (root / "rules").mkdir(parents=True)
        (root / "prompts").mkdir()
        (root / "rules" / "safety_rules.yaml").write_text("rules: 1\n")
        (root / "prompts" / "root_cause.v1.md").write_text("prompt v1\n")
        freeze_check.write_manifest(root=root)
        return root

    def test_unlisted_v2_file_fails_verify(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        assert freeze_check.verify(root=root)
        # an unregistered new prompt version must fail the freeze check
        (root / "prompts" / "root_cause.v2.md").write_text("prompt v2\n")
        assert not freeze_check.verify(root=root)
        (root / "prompts" / "root_cause.v2.md").unlink()
        assert freeze_check.verify(root=root)

    async def test_doctor_includes_freeze_check(self):
        from rsi.doctor import Doctor

        report = await Doctor().run_all()
        results = [r for r in report.results if r.name == "judge 冻结校验"]
        assert results, "doctor report must include the freeze check"
        # the repo manifest exists and is consistent -> pass
        assert results[0].status == "pass"


# ── 7. cassette prompt fingerprint ───────────────────────────────────────


class TestCassetteFingerprint:
    async def test_prompt_version_change_invalidates_cassette(self):
        lib = PromptLibrary(
            templates={"judge/root_cause.v1": "v1 template"}, component="judge"
        )
        calls: list[str] = []

        def handler(component, prompt_name, vars):
            calls.append(prompt_name)
            return '{"match": false}'

        client = LLMClient(handler=handler, prompt_library=lib)
        await client.complete("judge", "root_cause", {"run": "r1"})
        assert len(calls) == 1
        # same vars, same version -> cassette hit (no new handler call)
        await client.complete("judge", "root_cause", {"run": "r1"})
        assert len(calls) == 1
        # new prompt version -> fingerprint changes -> replay must NOT hit
        lib._templates["judge/root_cause.v2"] = "v2 template"
        await client.complete("judge", "root_cause", {"run": "r1"})
        assert len(calls) == 2


# ── 8. AuditLog persistence via bootstrap ────────────────────────────────


class TestAuditPersistence:
    async def test_build_services_audit_log_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        from rsi.bootstrap import build_services
        from rsi.db import init_db
        from rsi.db.tables import AuditLogEntry

        services = build_services()
        init_db(services["engine"], "admin", "x")
        services["audit_log"].record(
            actor="tester", action="user.login", object_type="user",
            object_id="u1", detail={"k": "v"},
        )
        with services["session_factory"]() as session:
            rows = session.query(AuditLogEntry).all()
        assert any(r.action == "user.login" for r in rows)


# ── 9. normalize_config anchored timestamps ──────────────────────────────


class TestNormalizeConfig:
    def test_mac_and_time_tokens_kept(self):
        text = (
            "sysname R1\n"
            "mac-address 00:11:22\n"
            "description uptime is fine\n"
            "2026-01-01 00:00:00 UTC\n"
            "clock is 12:00:00 UTC Sat 01/01/2026\n"
        )
        kept = normalize_config(text)
        assert "mac-address 00:11:22" in kept
        assert "description uptime is fine" in kept
        assert "sysname R1" in kept
        assert not any(l.startswith("2026-01-01") for l in kept)
        assert not any(l.lower().startswith("clock") for l in kept)


# ── 10e. prompt version numeric ordering ─────────────────────────────────


class TestPromptVersionOrdering:
    def test_v10_beats_v2(self):
        lib = PromptLibrary(
            templates={
                "judge/root_cause.v2": "two",
                "judge/root_cause.v10": "ten",
            },
            component="judge",
        )
        assert lib.latest_version("judge", "root_cause") == 10
        assert lib.render("judge", "root_cause", {}) == "ten"


# ── 10a. review gate requires explicit approval ──────────────────────────


class TestReviewGateExplicit:
    async def test_review_none_is_422_and_true_passes(self):
        auth = AuthService(secret="s")
        app = create_app({
            "auth_service": auth,
            "audit_log": AuditLog(),
            "users": {
                "admin": {
                    "user_id": "u-admin", "role": "admin",
                    "password_hash": auth.hash_password("pw"),
                }
            },
        })
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = await _login(client)
            app.state.stores["suts"]["sut-1"] = {"id": "sut-1"}
            resp = await client.post(
                "/api/v1/iterations",
                json={"sut_id": "sut-1", "profile_id": "p-1"},
                headers={"Authorization": f"Bearer {token}"},
            )
            iter_id = resp.json()["iteration_id"]
            store = app.state.stores["iterations"]
            # walk to awaiting_review: pass gate 1 + gate 2, then pause
            store[iter_id]["state"] = "paused"
            store[iter_id]["paused_from"] = "REVIEWING"
            store[iter_id]["current_step"] = "awaiting_review"
            resp = await client.post(
                f"/api/v1/iterations/{iter_id}/review",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 422, resp.text
            resp = await client.post(
                f"/api/v1/iterations/{iter_id}/review",
                json={"approved": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 204, resp.text


# ── 10d. safety rules malformed regex fallback ───────────────────────────


class TestSafetyFallback:
    def test_bad_regex_falls_back_to_builtin(self, tmp_path: Path):
        from rsi.judge.safety import SafetyScanner

        bad = tmp_path / "rules.yaml"
        bad.write_text(
            "read_only_prefixes: [display]\n"
            "config_delete_markers: [.cfg]\n"
            'config_mode_commands: ["system-view"]\n'
            "fatal:\n"
            '  - id: broken\n'
            '    regex: "([unclosed"\n',
            encoding="utf-8",
        )
        scanner = SafetyScanner(rules_path=bad)
        assert scanner.rules_from_file is False
        verdict = scanner.scan(["reset saved-configuration"], "read-only")
        assert verdict.fatal is True
