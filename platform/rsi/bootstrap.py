"""Production composition root (audit T2-1/T2-2/T2-4/B-16).

``build_services`` turns :class:`rsi.settings.Settings` into the full
service dict consumed by :func:`rsi.api.app.create_app` — the production
version of the hand-wired rig in ``tests/integration/test_closed_loop.py``.

Offline safety: no Docker, no LLM keys, no network and no destructive
filesystem actions are required or performed. Every external dependency
gets a Fake/mock default (FakeSandboxBackend, FakeCarrier/OpenCodeAdapter,
MockDeviceSession SSH pool, offline-mock LLM handler); GitStore is
constructed but *not* initialized (``.init()`` is the caller's decision).
"""

from __future__ import annotations

import base64
import functools
import json
import os
from functools import partial
from typing import Any, Callable

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.adapters.opencode import OpenCodeAdapter
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.decision.aggregator import StatsAggregator
from rsi.decision.noise import NoiseBandCalibrator
from rsi.decision.recorder import DecisionRecorder
from rsi.decision.selector import RegularizedSelector
from rsi.judge.aggregate import VerdictAggregator
from rsi.judge.antihack import AntiHackDetector
from rsi.judge.api import JudgeService
from rsi.judge.deterministic import DeterministicJudge
from rsi.judge.llm_judge import LLMJudge
from rsi.judge.safety import SafetyScanner
from rsi.judge.summarizer import TraceSummarizer
from rsi.llm.accounting import Accountant
from rsi.llm.client import LLMClient
from rsi.llm.prompts import PromptLibrary
from rsi.log import get_logger
from rsi.optimizer.applier import ChangeApplier
from rsi.optimizer.attribution import AttributionAnalyzer
from rsi.optimizer.critic import DeterministicCritic
from rsi.optimizer.sft import SftAssessor
from rsi.optimizer.targets import TargetRegistry
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.health import HealthMonitor, build_checkers
from rsi.orchestrator.iteration import DbPauseStore, IterationEngine
from rsi.orchestrator.resolver import build_batch_resolver, build_noise_run_batch_fn
from rsi.sandbox.backends.base import SandboxBackend
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.backends.prebuilt import PrebuiltBackend
from rsi.sandbox.config_api import ConfigApiCaller
from rsi.sandbox.platform_probe import PlatformProbe
from rsi.sandbox.readiness import ReadinessChecker
from rsi.sandbox.scheduler import LabScheduler
from rsi.sandbox.service import SandboxService
from rsi.sandbox.sshpool import SshPool
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.security.credentials import CredentialResolver, CredentialStore
from rsi.settings import Settings
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore
from rsi.versioning.gitstore import GitStore

__all__ = ["build_services", "bootstrap_database"]

logger = get_logger(__name__)

_EPHEMERAL_JWT_SECRET = "rsi-ephemeral-jwt-secret"

# Deployment-tier secrets (C1): fixed env-var names — the security section
# no longer exists in Settings; jwt_expire_hours is a deployment constant.
_JWT_SECRET_ENV = "RSI_JWT_SECRET"
_MASTER_KEY_ENV = "RSI_MASTER_KEY"
_JWT_EXPIRE_HOURS = 12


def _offline_llm_handler(component: str, prompt_name: str, vars: dict) -> str:
    """Deterministic offline handler: valid-schema payloads for the judge."""
    payloads = {
        "root_cause": json.dumps({"match": False, "confidence": 0.5}),
        "path_quality": json.dumps(
            {
                "verdict": "indeterminate",
                "skipped_critical_steps": [],
                "reasoning": "offline mock LLM (no provider key configured)",
            }
        ),
    }
    return payloads.get(prompt_name, json.dumps({"status": "offline_mock"}))


def bootstrap_database(
    settings: Settings | None = None,
    *,
    init: bool = False,
    admin_user: str = "admin",
    admin_password_hash: str = "",
) -> tuple[Any, Callable[[], Any]]:
    """Create the engine + session factory; optionally create tables.

    Returns ``(engine, session_factory)`` where ``session_factory()`` yields
    a context-managed :class:`sqlmodel.Session`. ``init=True`` also creates
    all tables and seeds the admin user (caller supplies the argon2 hash).
    """
    settings = settings or Settings()
    from rsi.db import create_engine, get_session, init_db

    engine = create_engine(settings.database_url)
    if init:
        init_db(engine, admin_user, admin_password_hash)
    return engine, functools.partial(get_session, engine)


def _master_key(settings: Settings) -> tuple[str, bool]:
    """(key, ephemeral) — fall back to a random per-process key offline."""
    candidate = os.environ.get(_MASTER_KEY_ENV, "")
    if candidate:
        try:
            CredentialStore(candidate)
            return candidate, False
        except Exception:
            logger.warning("credentials.invalid_master_key", env=_MASTER_KEY_ENV)
    return base64.b64encode(os.urandom(32)).decode(), True


def build_services(
    settings: Settings | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Composition root: Settings -> full service dict for ``create_app``."""
    settings = settings or Settings()

    # -- DB --------------------------------------------------------------
    engine = None
    if session_factory is None:
        engine, session_factory = bootstrap_database(settings)

    # -- Security --------------------------------------------------------
    # R2-M1: audit entries persist to the audit_log table via the session
    # factory (one short session per record; never a shared long-lived one).
    audit_log = AuditLog(session_factory=session_factory)
    master_key, ephemeral_creds = _master_key(settings)
    credential_store = CredentialStore(master_key)
    credential_resolver = CredentialResolver(store=credential_store)
    jwt_secret = os.environ.get(_JWT_SECRET_ENV, "") or (
        _EPHEMERAL_JWT_SECRET
    )
    auth_service = AuthService(
        secret=jwt_secret,
        expire_hours=_JWT_EXPIRE_HOURS,
        session_factory=session_factory,
        audit_log=audit_log,
    )

    # -- Versioning (constructed, NOT initialized: no git init here) ------
    target_registry = TargetRegistry()
    git_store = GitStore(
        repo_path=settings.git_repo_path,
        worktree_root=settings.git_worktrees_dir,
        session_factory=session_factory,
        audit_log=audit_log,
        target_registry=target_registry,
    )

    # -- Tracing (collector <-> store circular dep via attach_store) ------
    collector = TraceCollector(SeqAllocator(), None)
    trace_store = TraceStore(collector)
    collector.attach_store(trace_store)

    # -- LLM (offline mock handler; real providers plug in via handler) ---
    from pathlib import Path as _Path

    _prompts_dir = _Path(__file__).parent / "judge" / "prompts"
    prompt_library = PromptLibrary(template_dir=_prompts_dir, component="judge")
    accountant = Accountant(session_factory=session_factory)  # C-7: llm_calls rows
    judge_cfg = settings.llm.defaults.get("judge")
    llm_client = LLMClient(
        handler=_offline_llm_handler,
        max_concurrent_calls=settings.llm.max_concurrent_calls,
        # 本地 settings.yaml 可能只写 judge 的部分键（如仅 model）——
        # temperature/model 缺省回退，避免 None 进入 float()/LLM 调用。
        model=(judge_cfg.model or "mock") if judge_cfg else "mock",
        prompt_library=prompt_library,
        accountant=accountant,
        temperature=float(judge_cfg.temperature) if judge_cfg and judge_cfg.temperature is not None else 0.0,
    )

    # -- Judge (session_factory wired -> judgments table persistence) -----
    llm_judge = LLMJudge(llm_client, TraceSummarizer())
    judge_service = JudgeService(
        det=DeterministicJudge(timeout_s=float(settings.judge.det_timeout_s)),
        safety=SafetyScanner(),
        llm=llm_judge,
        antihack=AntiHackDetector(),
        agg=VerdictAggregator(),
        session_factory=session_factory,
        # C-1: freeze-audit fingerprint of the judge prompt actually used
        prompt_hash_fn=lambda: llm_client.prompt_hash("judge", "root_cause"),
    )

    # -- Carrier -----------------------------------------------------------
    host_configs = {
        name: cfg.model_dump() for name, cfg in settings.carrier.hosts.items()
    }
    host_pool = HostPool(host_configs)
    # Offline safety: a configured pool whose hosts are all unreachable
    # (no Docker daemon) degrades to the offline stub pool so the assembly
    # never requires a live daemon (check_reach never raises).
    try:
        if host_configs and not any(
            host_pool.check_reach(name) for name in host_configs
        ):
            host_pool = HostPool()  # offline stub mode
    except Exception:  # noqa: BLE001 - e.g. docker SDK absent
        host_pool = HostPool()
    net_policy = NetPolicy(host_pool)
    adapters: dict[str, Any] = {
        "opencode": OpenCodeAdapter(),
        "fake": FakeCarrier(),
    }
    host_service = HostService(
        host_pool=host_pool,
        net_policy=net_policy,
        adapters=adapters,
        settings=settings,
    )

    # -- Sandbox (T4: kind routing — fake offline, prebuilt platforms) ---
    # Bug fix (T4): every settings backend used to be setdefault'd to
    # FakeSandboxBackend, so env.backend="ensp-pro" had no real candidate.
    # Now each settings backend is dispatched on its ``kind``; prebuilt
    # platform backends (ensp-pro / pnetlab) share one SshPool +
    # ReadinessChecker with SandboxService (same (ip, port) session keys).
    ssh_pool = SshPool()  # default factory = MockDeviceSession (offline)
    readiness_checker = ReadinessChecker(pool=ssh_pool)
    backends: dict[str, SandboxBackend] = {"fake": FakeSandboxBackend()}
    platform_probes: dict[str, PlatformProbe] = {}
    for name, cfg in settings.sandbox.backends.items():
        if cfg.kind == "fake":
            backends.setdefault(name, FakeSandboxBackend())
            continue
        if not cfg.url:
            # No platform URL（物理网络 kind=physical 或未配 URL）：backend 保持
            # 构造，在线判定回退为设备 SSH 可达（PrebuiltBackend.status，
            # ADR-25 增补）——建成 lab 之前 status 报离线。
            logger.warning(
                "bootstrap.sandbox_backend_missing_url",
                backend=name,
                kind=cfg.kind,
            )
        probe = PlatformProbe(url=cfg.url, verify_tls=cfg.verify_tls)
        platform_probes[name] = probe
        backends[name] = PrebuiltBackend(
            key=name,
            kind=cfg.kind,
            probe=probe,
            ssh_pool=ssh_pool,
            readiness=readiness_checker,
            devices=cfg.devices,
        )

    sandbox_service = SandboxService(
        backends=backends,
        labs_dir=settings.labs_dir,
        config_api=ConfigApiCaller(credential_store=credential_store),
        scheduler=LabScheduler(backends),
        settings=settings,
        ssh_pool=ssh_pool,
        session_factory=session_factory,
        readiness_checker=readiness_checker,
    )

    # -- Decision / Optimizer ---------------------------------------------
    from rsi.decision.significance import SignificanceTester

    stats_agg = StatsAggregator()
    selector = RegularizedSelector(significance=SignificanceTester())
    noise_band = NoiseBandCalibrator(
        factor=settings.decision.noise.factor,
        session_factory=session_factory,
    )

    def _noise_lookup(evalset_id: str) -> dict[str, float]:
        """R2-H1: evalset -> {metric: sigma} from persisted noise_bands rows."""
        try:
            from rsi.db.tables import NoiseBand

            with session_factory() as session:
                rows = session.query(NoiseBand).filter(
                    NoiseBand.evalset_id == evalset_id
                )
                return {row.metric: float(row.sigma) for row in rows}
        except Exception:  # noqa: BLE001 - lookup must not kill DECIDING
            return {}

    decision_recorder = DecisionRecorder(
        session_factory=session_factory, audit_log=audit_log
    )
    attribution_analyzer = AttributionAnalyzer()
    deterministic_critic = DeterministicCritic(target_registry)
    change_applier = ChangeApplier(target_registry, gitstore=git_store)
    sft_assessor = SftAssessor(
        no_gain_rounds=settings.optimizer.sft.no_gain_rounds,
        min_samples=settings.optimizer.sft.min_samples,
        artifacts_dir=settings.artifacts_dir,
    )

    # -- Orchestrator -------------------------------------------------------
    event_bus = EventBus()
    checkpoint_store = CheckpointStore(
        interval_s=float(settings.orchestrator.checkpoint_interval_s)
    )
    budget_controller = BudgetController(
        stop_file=settings.stop_file,
        scope_limits={},
        global_limit=settings.orchestrator.budget.global_.max_tokens,
        event_bus=event_bus,
    )
    # B-1: Instrumentor facade — run_batch instruments exported config
    # versions (scan -> plan -> inject -> smoke verify) before create_instance.
    from rsi.instrumentor.facade import Instrumentor

    # D-10/N-2 (audit4): the app's in-memory stores must be owned here so
    # the batch resolver and create_app's app.state.stores share the same
    # dicts (routers register evalsets/suts; the resolver reads them).
    from rsi.api.deps import default_stores

    stores = default_stores(settings)
    batch_resolver = build_batch_resolver(
        session_factory=session_factory,
        gitstore=git_store,
        evalsets_store=stores["evalsets"],
        suts_store=stores["suts"],
    )
    batch_runner = BatchRunner(
        sandbox=sandbox_service,
        carrier=host_service,
        collector=collector,
        trace_store=trace_store,
        judge=judge_service,
        ckpt=checkpoint_store,
        budget=budget_controller,
        event_bus=event_bus,
        stats_agg=stats_agg,
        settings=settings,
        credential_resolver=credential_resolver,
        instrumentor=Instrumentor(),
        session_factory=session_factory,
        carrier_type="opencode",
        batch_resolver=batch_resolver,
    )
    # N-3 (audit4): noise-calibration run_batch_fn factory — the
    # /dashboard/noise-bands:calibrate endpoint calls it per (evalset, sut).
    noise_run_batch_fn = partial(
        build_noise_run_batch_fn, batch_resolver, batch_runner
    )
    # D-4 (设计 §5.4): PAUSED 落库 — DbPauseStore persists gate pause records
    # to the iterations table so the PAUSED state + gate step survive a
    # process restart (queryable via API; engine start warns on stale
    # records). Falls back to the in-memory store only when no DB session
    # factory is available.
    pause_store = (
        DbPauseStore(session_factory) if session_factory is not None else None
    )
    iteration_engine = IterationEngine(
        batch_runner=batch_runner,
        attribution_analyzer=attribution_analyzer,
        selector=selector,
        event_bus=event_bus,
        budget=budget_controller,
        ckpt=checkpoint_store,
        # F10 wiring: optimizer loop components built above must reach the
        # engine — APPLYING needs the real ChangeApplier (candidate versions),
        # DECIDING needs critic + DecisionRecorder (experiments rows) and the
        # GitStore for promote/rollback on the production chain.
        change_applier=change_applier,
        critic=deterministic_critic,
        decision_recorder=decision_recorder,
        gitstore=git_store,
        noise_lookup=_noise_lookup,
        state_store=pause_store,
        # U7b: per-SUT optimization scope (suts store record) narrows RSI
        # auto-optimization to the explicitly defined paths; missing/empty
        # = automatic mode (platform scans and decides, previous behaviour).
        sut_scope_fn=lambda sut_id: stores["suts"]
        .get(sut_id, {})
        .get("optimization_scope"),
    )

    # -- HealthMonitor (D-1, DD-12 §4.7) -----------------------------------
    # built last so every probed component is already in the services dict.
    services: dict[str, Any] = {
        # infra
        "settings": settings,
        "engine": engine,
        "session_factory": session_factory,
        # security
        "auth_service": auth_service,
        "audit_log": audit_log,
        "credential_store": credential_store,
        "credential_resolver": credential_resolver,
        # versioning
        "git_store": git_store,
        # tracing
        "tracing_collector": collector,
        "trace_store": trace_store,
        # llm
        "prompt_library": prompt_library,
        "accountant": accountant,
        "llm_client": llm_client,
        "llm_judge": llm_judge,
        # judge
        "judge_service": judge_service,
        # carrier
        "host_service": host_service,
        "host_pool": host_pool,
        "net_policy": net_policy,
        # sandbox
        "sandbox_service": sandbox_service,
        "ssh_pool": ssh_pool,
        # T4: platform URL probes per backend key (health / doctor share these)
        "platform_probes": platform_probes,
        # decision
        "stats_agg": stats_agg,
        "selector": selector,
        "noise_band": noise_band,
        "decision_recorder": decision_recorder,
        # optimizer
        "target_registry": target_registry,
        "attribution_analyzer": attribution_analyzer,
        "deterministic_critic": deterministic_critic,
        "change_applier": change_applier,
        "sft_assessor": sft_assessor,
        # orchestrator
        "event_bus": event_bus,
        "checkpoint_store": checkpoint_store,
        "budget_controller": budget_controller,
        "batch_runner": batch_runner,
        "iteration_engine": iteration_engine,
        # audit4 D-10/N-2: app-owned in-memory stores (shared with the
        # batch resolver) + N-3 noise-calibration bridge factory
        "stores": stores,
        "noise_run_batch_fn": noise_run_batch_fn,
        # api compatibility (users store for per-request disable checks)
        "users": {},
    }
    health_monitor = HealthMonitor(
        checkers=build_checkers(services),
        interval_s=float(settings.orchestrator.health_monitor_interval_s),
        event_bus=event_bus,
    )
    services["health_monitor"] = health_monitor

    logger.info(
        "bootstrap.assembled",
        services="full",
        ephemeral_creds=ephemeral_creds,
        ephemeral_jwt=jwt_secret == _EPHEMERAL_JWT_SECRET,
    )
    return services
