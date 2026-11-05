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
from rsi.orchestrator.iteration import IterationEngine
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.config_api import ConfigApiCaller
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
    env_name = settings.security.master_key_env
    candidate = os.environ.get(env_name, "")
    if candidate:
        try:
            CredentialStore(candidate)
            return candidate, False
        except Exception:
            logger.warning("credentials.invalid_master_key", env=env_name)
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
    audit_log = AuditLog()  # in-memory P0; DB persistence is the F5 slice
    master_key, ephemeral_creds = _master_key(settings)
    credential_store = CredentialStore(master_key)
    credential_resolver = CredentialResolver(store=credential_store)
    jwt_secret = os.environ.get(settings.security.jwt_secret_env, "") or (
        _EPHEMERAL_JWT_SECRET
    )
    auth_service = AuthService(
        secret=jwt_secret,
        expire_hours=settings.security.jwt_expire_hours,
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
    llm_client = LLMClient(
        handler=_offline_llm_handler,
        max_concurrent_calls=settings.llm.max_concurrent_calls,
        model=settings.llm.defaults.get("judge").model
        if settings.llm.defaults.get("judge")
        else "mock",
        prompt_library=prompt_library,
        accountant=accountant,
        temperature=float(
            settings.llm.defaults.get("judge").temperature
            if settings.llm.defaults.get("judge")
            else 0.0
        ),
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

    # -- Sandbox (P0: Fake backends; real backends land in S11) ----------
    ssh_pool = SshPool()  # default factory = MockDeviceSession (offline)
    backends: dict[str, Any] = {"fake": FakeSandboxBackend()}
    for name in settings.sandbox.backends:
        backends.setdefault(name, FakeSandboxBackend())
    from rsi.sandbox.readiness import ReadinessChecker

    sandbox_service = SandboxService(
        backends=backends,
        labs_dir=settings.labs_dir,
        config_api=ConfigApiCaller(credential_store=credential_store),
        scheduler=LabScheduler(backends),
        settings=settings,
        ssh_pool=ssh_pool,
        session_factory=session_factory,
        readiness_checker=ReadinessChecker(pool=ssh_pool),
    )

    # -- Decision / Optimizer ---------------------------------------------
    from rsi.decision.significance import SignificanceTester

    stats_agg = StatsAggregator()
    selector = RegularizedSelector(significance=SignificanceTester())
    noise_band = NoiseBandCalibrator(
        factor=settings.decision.noise.factor,
        session_factory=session_factory,
    )
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
