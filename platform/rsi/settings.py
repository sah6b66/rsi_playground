"""Platform settings (config doc §2, RSI-PG-DES-002).

Pydantic v2 BaseSettings; env var prefix ``RSI_`` with ``__`` nesting
(e.g. ``RSI_SERVER__PORT``). ``RSI_HOME`` (default ``./var``) is the base
for resolving relative paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_HOME = "./var"

_C = ConfigDict(extra="ignore", validate_assignment=True)


# -- dotted-path access on Settings trees (system settings hot-update) ------


def get_dotted(obj: object, path: str) -> object:
    """Read a ``a.b.c`` leaf from nested BaseModels / dicts (KeyError /
    AttributeError propagate for unknown paths)."""
    for part in path.split("."):
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


def set_dotted(obj: object, path: str, value: object) -> object:
    """Write a ``a.b.c`` leaf on nested BaseModels / dicts.

    For pydantic model leaves the raw (often str) value is converted to
    the field's annotation via ``TypeAdapter`` first; ``setattr`` then
    re-validates (model_config has validate_assignment). Raises
    ``pydantic.ValidationError`` on invalid values — callers map it to
    API 422. Returns the converted value.
    """
    parts = path.split(".")
    parent = obj
    for part in parts[:-1]:
        parent = parent[part] if isinstance(parent, dict) else getattr(parent, part)
    leaf = parts[-1]
    if isinstance(parent, dict):
        parent[leaf] = value
        return value
    field = type(parent).model_fields.get(leaf)
    converted = (
        TypeAdapter(field.annotation).validate_python(value) if field else value
    )
    setattr(parent, leaf, converted)
    return converted


def _resolve(home: Path, value: str | Path) -> Path:
    """Resolve *value* against *home* if relative, else keep absolute."""
    p = Path(value)
    return p if p.is_absolute() else (home / p)


def _rsi_home() -> Path:
    return Path(os.environ.get("RSI_HOME", DEFAULT_HOME)).resolve()


def settings_yaml_path(home: Path | None = None) -> Path:
    """Locate the settings.yaml to persist static config edits to.

    Resolution order: ``RSI_SETTINGS`` env var (absolute, or relative to
    *home*), else ``<home>/settings.yaml`` (*home* = RSI_HOME env default).
    """
    home = home or _rsi_home()
    override = os.environ.get("RSI_SETTINGS")
    if override:
        p = Path(override)
        return p if p.is_absolute() else home / p
    return home / "settings.yaml"


def write_settings_section(section_key: str, data: object, *, home: Path | None = None) -> Path:
    """Rewrite one dotted section (e.g. ``carrier.hosts``) in settings.yaml.

    Conservative round-trip: yaml.safe_load the whole document (missing
    file → empty doc), replace only the targeted section, safe_dump back
    with an atomic tmp-file + os.replace so a crash never truncates the
    config. Existing unrelated sections are preserved verbatim (values,
    not comments). Runtime Settings objects are NOT reloaded — callers
    surface ``restart_required`` to the user instead.
    """
    path = settings_yaml_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {}
    if path.is_file():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        doc = {}
    node = doc
    keys = section_key.split(".")
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = data
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


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


class DeviceEndpoint(BaseModel):
    """平台级设备端点（仅监控寻址用：``ip:port``）。

    凭证（username/password）仍属各评测集 ``env.yaml`` ``ssh_access``
    （ADR-25 职责划分：平台注册信息只管"设备在哪"，评测集管"怎么登录"）。
    """

    model_config = _C
    ip: str
    port: int = 22


class SandboxBackend(BaseModel):
    """§2 sandbox.backends.* — simulation-platform (Web) backend def.

    eNSP Pro / PNETLab are web platforms (no platform-level SSH); platform
    health is monitored via URL probing. Device-level SSH addresses live in
    each evalset's ``env.yaml`` ``ssh_access`` (same IP reused across devices
    with different ports; device IPs may differ from the platform URL IP).
    """

    model_config = _C
    kind: Literal["ensp-pro", "pnetlab", "physical", "fake"] = "fake"
    url: str = ""  # platform Web entry, e.g. https://192.168.56.101:8443
    verify_tls: bool = False  # self-signed certs off by default
    api: Optional[str] = None  # reserved for S11 Web-API orchestration
    subnets: list[str] = Field(default_factory=list)
    # 平台级设备清单（ADR-25 增补）：未配 URL（物理网络）时平台在线判定
    # 优先探测这些端点（任一 SSH 可达即在线），与在用 lab 无关。
    devices: dict[str, DeviceEndpoint] = Field(default_factory=dict)


class SandboxSettings(BaseModel):
    """§2 sandbox — simulation environment abstraction."""

    model_config = _C
    backends: dict[str, SandboxBackend] = Field(default_factory=lambda: {
        "fake": SandboxBackend(kind="fake"),
        "ensp-pro-1": SandboxBackend(
            kind="ensp-pro",
            url="https://192.168.56.101:8443",
            subnets=["10.101.0.0/16"],
        ),
        "pnetlab": SandboxBackend(
            kind="pnetlab",
            url="http://192.168.56.102",
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
    max_instances: Optional[int] = None  # per-host override (None → global default)


class CarrierSettings(BaseModel):
    """§2 carrier — SUT hosting adapter layer."""

    model_config = _C
    task_timeout_s: int = 1800
    max_instances_per_host: int = 10  # scheduler default (per-host override wins)
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
    health_monitor_interval_s: float = 30.0  # DD-12 §4.7 poll period
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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Priority: init kwargs > env (RSI_*) > settings.yaml > defaults.

        The yaml file lives at ``settings_yaml_path()`` (RSI_SETTINGS or
        ``<RSI_HOME>/settings.yaml``); a missing file yields an empty
        source, so settings.yaml is now authoritative at runtime — not
        just a doctor-checked document.
        """
        yaml_source = YamlConfigSettingsSource(
            settings_cls, yaml_file=str(settings_yaml_path())
        )
        return (
            init_settings,
            env_settings,
            yaml_source,
            dotenv_settings,
            file_secret_settings,
        )

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
