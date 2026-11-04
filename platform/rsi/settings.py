"""Platform settings (config doc §2, RSI-PG-DES-002).

Pydantic v2 BaseSettings; env var prefix ``RSI_`` with ``__`` nesting
(e.g. ``RSI_SERVER__PORT``). ``RSI_HOME`` (default ``./var``) is the base
for resolving relative paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOME = "./var"

_C = ConfigDict(extra="ignore", validate_assignment=True)


def _resolve(home: Path, value: str | Path) -> Path:
    """Resolve *value* against *home* if relative, else keep absolute."""
    p = Path(value)
    return p if p.is_absolute() else (home / p)


def _rsi_home() -> Path:
    return Path(os.environ.get("RSI_HOME", DEFAULT_HOME)).resolve()


# ════════ ① Infrastructure (config doc §2 ①) ════════


class ServerSettings(BaseModel):
    """§2 server — Platform API listen config."""

    model_config = _C
    host: str = "0.0.0.0"
    port: int = 8000


class DatabaseSettings(BaseModel):
    """§2 database — P0 SQLite, P1 PostgreSQL."""

    model_config = _C
    url: str = "sqlite:///platform.db"  # concrete form via Settings.database_url


class GitRepoSettings(BaseModel):
    """§2 git.repo — bare repo for pre-receive freeze enforcement."""

    model_config = _C
    path: Path = Path("git/platform-repo.git")
    bare: bool = True


class GitAuthorSettings(BaseModel):
    model_config = _C
    name: str = "rsi-platform"
    email: str = "rsi-platform@local"


class GitSettings(BaseModel):
    """§2 git — GitStore repo, worktrees, commit identity."""

    model_config = _C
    repo: GitRepoSettings = GitRepoSettings()
    worktrees: Path = Path("git/wt")
    author: GitAuthorSettings = GitAuthorSettings()


class StorageSettings(BaseModel):
    """§2 storage — traces / artifacts / labs / recordings / stop file."""

    model_config = _C
    traces_dir: Path = Path("traces")
    artifacts_dir: Path = Path("artifacts")
    labs_dir: Path = Path("labs")
    recordings_dir: Path = Path("recordings")
    stop_file: Path = Path("STOP")


class LoggingSettings(BaseModel):
    """§2 logging — structured logs, daily rotation, error split."""

    model_config = _C
    dir: Path = Path("logs")
    level: Literal["debug", "info", "warning", "error"] = "info"
    retention_days: int = 30
    error_split: bool = True


# ════════ ② sandbox (config doc §2 ②) ════════


class SandboxBackend(BaseModel):
    """§2 sandbox.backends.* — self-contained backend resource def."""

    model_config = _C
    host: str = ""
    ssh_port: int = 22
    ssh_user: str = "root"
    agent_cmd: Optional[str] = None
    templates_dir: Optional[str] = None
    api: Optional[str] = None
    subnets: list[str] = Field(default_factory=list)


class SandboxSettings(BaseModel):
    """§2 sandbox — simulation environment abstraction."""

    model_config = _C
    backends: dict[str, SandboxBackend] = Field(default_factory=lambda: {
        "ensp-pro-1": SandboxBackend(
            host="192.168.x.x",
            ssh_user="administrator",
            agent_cmd="pg-enspctl",
            templates_dir="deploy/ensp-templates",
            subnets=["10.101.0.0/16"],
        ),
        "pnetlab": SandboxBackend(
            host="192.168.x.y",
            ssh_user="root",
            subnets=["10.102.0.0/16"],
        ),
    })
    build_retry: int = 2
    ready_timeout_s: int = 600


# ════════ ③ carrier (config doc §2 ③) ════════


class CarrierAdapterSettings(BaseModel):
    """§2 carrier.adapters.* — base image for CarrierAdapter.build_image."""

    model_config = _C
    image_base: str = ""


class CarrierHostSettings(BaseModel):
    """§2 carrier.hosts.* — Docker execution host pool entry."""

    model_config = _C
    docker: str = "local"
    subnets: list[str] = Field(default_factory=list)


class CarrierSettings(BaseModel):
    """§2 carrier — SUT hosting adapter layer."""

    model_config = _C
    task_timeout_s: int = 1800
    adapters: dict[str, CarrierAdapterSettings] = Field(default_factory=lambda: {
        "claude_code": CarrierAdapterSettings(image_base="rsi/claude-code-runtime:1.x"),
        "opencode": CarrierAdapterSettings(image_base="rsi/opencode-runtime:1.x"),
    })
    hosts: dict[str, CarrierHostSettings] = Field(default_factory=lambda: {
        "main": CarrierHostSettings(docker="local", subnets=["10.100.0.0/16"]),
    })


# ════════ ④ instrumentor (config doc §2 ④) ════════


class InstrumentorPolicySettings(BaseModel):
    """§2 instrumentor.policy — injection preference order + smoke."""

    model_config = _C
    prefer: list[str] = Field(
        default_factory=lambda: ["hooks", "mcp_wrap", "kb_proxy", "source"]
    )
    source_injection: bool = True
    smoke_verify: bool = True


class InstrumentorSettings(BaseModel):
    model_config = _C
    policy: InstrumentorPolicySettings = InstrumentorPolicySettings()


# ════════ ⑤ tracing (config doc §2 ⑤) ════════


class TracingCollectorSettings(BaseModel):
    """§2 tracing.collector — OTLP receivers."""

    model_config = _C
    listen: str = "0.0.0.0"
    grpc: int = 4317
    http: int = 4318


class TracingRedactSettings(BaseModel):
    model_config = _C
    enabled: bool = True
    rules_path: Path = Path("redact-rules.yaml")


class TracingArchiveSettings(BaseModel):
    model_config = _C
    codec: Literal["zstd", "gzip", "none"] = "zstd"
    hot_days: int = 90
    keep_promoted_forever: bool = True


class TracingSettings(BaseModel):
    """§2 tracing — trace subsystem."""

    model_config = _C
    collector: TracingCollectorSettings = TracingCollectorSettings()
    redact: TracingRedactSettings = TracingRedactSettings()
    archive: TracingArchiveSettings = TracingArchiveSettings()


# ════════ ⑥ judge (config doc §2 ⑥) ════════


class JudgeSettings(BaseModel):
    """§2 judge — evaluation judge runtime knobs."""

    model_config = _C
    det_timeout_s: int = 120
    llm_retry: int = 2
    pairwise_enabled: bool = True


# ════════ ⑦ optimizer (config doc §2 ⑦) ════════


class OptimizerSftSettings(BaseModel):
    """§2 optimizer.sft — SFT admission thresholds."""

    model_config = _C
    no_gain_rounds: int = 5
    min_samples: int = 50


class OptimizerSettings(BaseModel):
    """§2 optimizer — optimization engine."""

    model_config = _C
    critic_max_repair_rounds: int = 5
    sft: OptimizerSftSettings = OptimizerSftSettings()


# ════════ ⑧ decision (config doc §2 ⑧) ════════


class DecisionNoiseSettings(BaseModel):
    """§2 decision.noise — noise band calibration."""

    model_config = _C
    k_repeats: int = 3
    factor: float = 1.96


class DecisionSettings(BaseModel):
    """§2 decision — iteration decision."""

    model_config = _C
    noise: DecisionNoiseSettings = DecisionNoiseSettings()
    alpha: float = 0.05
    no_gain_rounds_default: int = 5


# ════════ ⑨ orchestrator (config doc §2 ⑨) ════════


class BudgetSettings(BaseModel):
    """§2 orchestrator.budget.* — token / wall / cost budgets."""

    model_config = _C
    max_tokens: int = 50_000_000
    max_wall_hours: float = 8.0
    max_cost_usd: float = 200.0


class OrchestratorBudgetSettings(BaseModel):
    model_config = _C
    default_iteration: BudgetSettings = BudgetSettings(
        max_tokens=50_000_000, max_wall_hours=8.0, max_cost_usd=200.0
    )
    global_: BudgetSettings = Field(
        default=BudgetSettings(
            max_tokens=100_000_000, max_wall_hours=0.0, max_cost_usd=400.0
        ),
        alias="global",
    )


class OrchestratorSettings(BaseModel):
    """§2 orchestrator — orchestration engine."""

    model_config = _C
    max_parallel_batches: int = 2
    checkpoint_interval_s: int = 5
    budget: OrchestratorBudgetSettings = OrchestratorBudgetSettings()


# ════════ ⑩ llm (config doc §2 ⑩) ════════


class LlmProviderSettings(BaseModel):
    """§2 llm.providers.* — platform-side LLM provider credentials."""

    model_config = _C
    api_key_env: str = ""
    base_url_env: Optional[str] = None


class LlmModelSettings(BaseModel):
    model_config = _C
    model: str = ""
    temperature: Optional[float] = None


class LlmSettings(BaseModel):
    """§2 llm — platform LLM (judge/attribution/hypothesis/critic)."""

    model_config = _C
    providers: dict[str, LlmProviderSettings] = Field(default_factory=lambda: {
        "anthropic": LlmProviderSettings(api_key_env="ANTHROPIC_API_KEY"),
        "openai_compat": LlmProviderSettings(
            api_key_env="OPENAI_COMPAT_API_KEY",
            base_url_env="OPENAI_COMPAT_BASE_URL",
        ),
    })
    defaults: dict[str, LlmModelSettings] = Field(default_factory=lambda: {
        "judge": LlmModelSettings(model="", temperature=0.0),
        "attribution": LlmModelSettings(model=""),
        "hypothesis": LlmModelSettings(model=""),
        "critic": LlmModelSettings(model=""),
    })
    cassette_mode: Literal["replay", "record", "off"] = "replay"
    max_concurrent_calls: int = 4


# ════════ ⑪ security (config doc §2 ⑪) ════════


class SecuritySettings(BaseModel):
    """§2 security — JWT + CredentialStore master key env names."""

    model_config = _C
    jwt_secret_env: str = "RSI_JWT_SECRET"
    master_key_env: str = "RSI_MASTER_KEY"
    jwt_expire_hours: int = 12


# ════════ ⑫ scheduler & seed (config doc §2 ⑫) ════════


class SchedulerSettings(BaseModel):
    """§2 scheduler — P0/P1 asyncio, P2 rq."""

    model_config = _C
    kind: Literal["asyncio", "rq"] = "asyncio"
    redis_url: Optional[str] = None


class SeedSettings(BaseModel):
    """§2 seed — global master seed for three-stage derivation."""

    model_config = _C
    master: int = 20261102


class Settings(BaseSettings):
    """Root platform settings (config doc §2)."""

    model_config = SettingsConfigDict(
        env_prefix="RSI_",
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
        populate_by_name=True,
    )

    server: ServerSettings = ServerSettings()
    database: DatabaseSettings = DatabaseSettings()
    git: GitSettings = GitSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
    sandbox: SandboxSettings = SandboxSettings()
    carrier: CarrierSettings = CarrierSettings()
    instrumentor: InstrumentorSettings = InstrumentorSettings()
    tracing: TracingSettings = TracingSettings()
    judge: JudgeSettings = JudgeSettings()
    optimizer: OptimizerSettings = OptimizerSettings()
    decision: DecisionSettings = DecisionSettings()
    orchestrator: OrchestratorSettings = OrchestratorSettings()
    llm: LlmSettings = LlmSettings()
    security: SecuritySettings = SecuritySettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    seed: SeedSettings = SeedSettings()

    # RSI_HOME, read once at construction (env var wins; default ./var).
    home: Path = Field(default_factory=_rsi_home)

    def model_post_init(self, __context) -> None:
        # Resolve relative paths in nested sections against RSI_HOME so
        # downstream consumers always see absolute paths (config doc §2 ①).
        self.storage.traces_dir = _resolve(self.home, self.storage.traces_dir)
        self.storage.artifacts_dir = _resolve(self.home, self.storage.artifacts_dir)
        self.storage.labs_dir = _resolve(self.home, self.storage.labs_dir)
        self.storage.recordings_dir = _resolve(self.home, self.storage.recordings_dir)
        self.storage.stop_file = _resolve(self.home, self.storage.stop_file)
        self.logging.dir = _resolve(self.home, self.logging.dir)
        self.git.repo.path = _resolve(self.home, self.git.repo.path)
        self.git.worktrees = _resolve(self.home, self.git.worktrees)

    @property
    def database_url(self) -> str:
        """Concrete DB URL (SQLite default under RSI_HOME)."""
        return self.database.url or f"sqlite:///{self.home / 'platform.db'}"

    def resolve(self, value: str | Path) -> Path:
        """Resolve a possibly-relative path against RSI_HOME."""
        return _resolve(self.home, value)

    # Resolved path properties for the storage/git/logging sections.
    @property
    def traces_dir(self) -> Path:
        return self.resolve(self.storage.traces_dir)

    @property
    def artifacts_dir(self) -> Path:
        return self.resolve(self.storage.artifacts_dir)

    @property
    def labs_dir(self) -> Path:
        return self.resolve(self.storage.labs_dir)

    @property
    def recordings_dir(self) -> Path:
        return self.resolve(self.storage.recordings_dir)

    @property
    def stop_file(self) -> Path:
        return self.resolve(self.storage.stop_file)

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.logging.dir)

    @property
    def git_repo_path(self) -> Path:
        return self.resolve(self.git.repo.path)

    @property
    def git_worktrees_dir(self) -> Path:
        return self.resolve(self.git.worktrees)
