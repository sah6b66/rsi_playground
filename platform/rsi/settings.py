"""Platform settings — three-tier configuration (config refactor C1).

Tiers (highest priority first):

1. **Deployment env vars** — ``RSI_*`` (e.g. ``RSI_SERVER__PORT``) and the
   deployment secrets ``RSI_JWT_SECRET`` / ``RSI_MASTER_KEY`` / ``RSI_HOME``.
2. **User config** — ``<RSI_HOME>/settings.yaml`` (~35 user-facing keys,
   shown by GET /system/settings and editable via PUT; object pages manage
   ``sandbox.backends`` / ``carrier.hosts`` in the same file).
3. **Internal config** — ``<RSI_HOME>/settings-internal.yaml`` (operator
   knobs; may override defaults but never user-yaml or env values).
4. Field defaults.

Derived filesystem layout (no longer configurable): all platform state
lives under ``RSI_HOME`` (default ``./var``):

    <home>/git/platform-repo.git   <home>/git/wt
    <home>/storage/{traces,artifacts,labs,recordings}
    <home>/logs

``RSI_HOME`` itself is not a Settings field; it is read from the
environment (deployment tier) and exposed via the ``home`` property.
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

# ── three-tier key classification ─────────────────────────────────────────
#
# USER_CONFIG_PREFIXES: flattened dotted-path prefixes allowed in GET
# /system/settings (the user-facing settings page). Everything else in the
# Settings tree is either object-page managed (sandbox.backends.*,
# carrier.hosts.*) or internal.
USER_CONFIG_PREFIXES: tuple[str, ...] = (
    "server.",
    "database.",
    # llm.providers.* is deliberately NOT user-page-visible: it carries
    # api_key credentials in plaintext — configure via settings.yaml / env.
    "llm.defaults.",
    "llm.cassette_mode",
    "llm.max_concurrent_calls",
    "judge.",
    "optimizer.",
    "decision.",
    "orchestrator.",
    "carrier.task_timeout_s",
    "carrier.max_instances_per_host",
    "sandbox.build_retry",
    "sandbox.ready_timeout_s",
    "tracing.redact.enabled",
    "tracing.archive.hot_days",
    "logging.level",
    "logging.retention_days",
    "logging.error_split",
    "seed.",
)


def is_user_config_key(path: str) -> bool:
    """True when a flattened Settings path belongs to the user config tier."""
    return any(path.startswith(p) for p in USER_CONFIG_PREFIXES)


# INTERNAL_KEYS: sourced from settings-internal.yaml only. The same keys in
# the *user* settings.yaml are ignored (internal config never wins from the
# user file), and they are never shown by GET /system/settings.
_INTERNAL_PREFIXES = (
    "instrumentor.",
    "scheduler.",
    "security.",
    "git.",
    "carrier.adapters.",
    "tracing.collector.",
)
_INTERNAL_EXACT = (
    "tracing.redact.rules_path",
    "tracing.archive.codec",
    "tracing.archive.keep_promoted_forever",
)

#: Keys read from settings-internal.yaml (documentation / load-side marker).
INTERNAL_KEYS: tuple[str, ...] = (
    "instrumentor.policy.prefer",
    "instrumentor.policy.source_injection",
    "instrumentor.policy.smoke_verify",
    "tracing.collector.listen",
    "tracing.collector.grpc",
    "tracing.collector.http",
    "tracing.redact.rules_path",
    "tracing.archive.codec",
    "tracing.archive.keep_promoted_forever",
    "scheduler.kind",
    "scheduler.redis_url",
    "security.jwt_secret_env",
    "security.master_key_env",
    "git.repo.bare",
    "git.author.name",
    "git.author.email",
    "carrier.adapters.claude_code.image_base",
    "carrier.adapters.opencode.image_base",
)


def is_internal_key(path: str) -> bool:
    """True when a flattened Settings path is internal-tier."""
    return path in _INTERNAL_EXACT or any(
        path.startswith(p) for p in _INTERNAL_PREFIXES
    )


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


def settings_internal_yaml_path(home: Path | None = None) -> Path:
    """Locate settings-internal.yaml (internal config tier).

    Resolution order: ``RSI_INTERNAL_SETTINGS`` env var (absolute, or
    relative to *home*), else ``<home>/settings-internal.yaml``. A missing
    file yields an empty source (field defaults apply).
    """
    home = home or _rsi_home()
    override = os.environ.get("RSI_INTERNAL_SETTINGS")
    if override:
        p = Path(override)
        return p if p.is_absolute() else home / p
    return home / "settings-internal.yaml"


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


def _filter_tree(node: Any, prefix: str, keep_internal: bool) -> Any:
    """Recursively keep only internal (or only non-internal) leaves.

    A dict node survives when at least one leaf beneath it survives, so a
    mixed section like ``tracing`` keeps its user leaves in the user tier
    and its internal leaves in the internal tier; a purely internal
    section like ``instrumentor`` survives only in the internal tier.
    """
    if not isinstance(node, dict) or not node:
        return node if is_internal_key(prefix) == keep_internal else None
    out: dict = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            sub = _filter_tree(value, path, keep_internal)
            if sub:
                out[key] = sub
        elif is_internal_key(path) == keep_internal:
            out[key] = value
    return out


class _TierYamlConfigSettingsSource(YamlConfigSettingsSource):
    """Yaml source restricted to one configuration tier.

    ``keep_internal=False`` (default): the *user* settings.yaml source —
    internal-tier keys found in the user file are dropped (the user file
    cannot set internal config). ``keep_internal=True``: the
    settings-internal.yaml source — only internal-tier keys are honoured.
    """

    def __init__(self, *args: Any, keep_internal: bool = False, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._keep_internal = keep_internal

    def __call__(self) -> dict[str, Any]:
        data = super().__call__() or {}
        filtered = _filter_tree(data, "", self._keep_internal)
        return filtered if isinstance(filtered, dict) else {}


# ══════ ① Infrastructure (config doc §2 ①) ══════


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
    """git.repo — bare flag only (repo path is derived from RSI_HOME)."""

    model_config = _C
    bare: bool = True


class GitAuthorSettings(BaseModel):
    """git.author — commit identity (internal config)."""

    model_config = _C
    name: str = "rsi-platform"
    email: str = "rsi-platform@local"


class GitSettings(BaseModel):
    """git — commit identity + bare flag; paths derived from RSI_HOME."""

    model_config = _C
    repo: GitRepoSettings = GitRepoSettings()
    author: GitAuthorSettings = GitAuthorSettings()


class LoggingSettings(BaseModel):
    """§2 logging — level / retention / error split (dir is derived)."""

    model_config = _C
    level: Literal["debug", "info", "warning", "error"] = "info"
    retention_days: int = 30
    error_split: bool = True


# ══════ ② sandbox (config doc §2 ②) ══════


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


# ══════ ③ carrier (config doc §2 ③) ══════


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


# ══════ ④ instrumentor (config doc §2 ④; internal config) ══════


class InstrumentorPolicySettings(BaseModel):
    """§2 instrumentor.policy — injection preference order + smoke."""

    model_config = _C
    prefer: list[str] = Field(
        default_factory=lambda: ["hooks", "mcp_wrap", "kb_proxy", "source"]
    )
    source_injection: bool = True
    smoke_verify: bool = True


class InstrumentorSettings(BaseModel):
    """§2 instrumentor — internal config (settings-internal.yaml)."""

    model_config = _C
    policy: InstrumentorPolicySettings = InstrumentorPolicySettings()


# ══════ ⑤ tracing (config doc §2 ⑤) ══════


class TracingCollectorSettings(BaseModel):
    """§2 tracing.collector — OTLP receivers (internal config)."""

    model_config = _C
    listen: str = "0.0.0.0"
    grpc: int = 4317
    http: int = 4318


class TracingRedactSettings(BaseModel):
    model_config = _C
    enabled: bool = True
    rules_path: Path = Path("redact-rules.yaml")  # internal


class TracingArchiveSettings(BaseModel):
    model_config = _C
    codec: Literal["zstd", "gzip", "none"] = "zstd"  # internal
    hot_days: int = 90
    keep_promoted_forever: bool = True  # internal


class TracingSettings(BaseModel):
    """§2 tracing — trace subsystem."""

    model_config = _C
    collector: TracingCollectorSettings = TracingCollectorSettings()
    redact: TracingRedactSettings = TracingRedactSettings()
    archive: TracingArchiveSettings = TracingArchiveSettings()


# ══════ ⑥ judge (config doc §2 ⑥) ══════


class JudgeSettings(BaseModel):
    """§2 judge — evaluation judge runtime knobs."""

    model_config = _C
    det_timeout_s: int = 120
    llm_retry: int = 2
    pairwise_enabled: bool = True


# ══════ ⑦ optimizer (config doc §2 ⑦) ══════


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


# ══════ ⑧ decision (config doc §2 ⑧) ══════


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


# ══════ ⑨ orchestrator (config doc §2 ⑨) ══════


class BudgetSettings(BaseModel):
    """§2 orchestrator.budget.* — token / wall / cost budgets."""

    model_config = _C
    max_tokens: int = 50_000_000
    max_wall_hours: float = 8.0
    max_cost_usd: float = 200.0


class OrchestratorBudgetSettings(BaseModel):
    # yaml key ``iteration_default`` (renamed from ``default_iteration``)
    iteration_default: BudgetSettings = BudgetSettings(
        max_tokens=50_000_000, max_wall_hours=8.0, max_cost_usd=200.0
    )
    # yaml key ``global`` (``global`` is a Python keyword; attribute stays
    # ``global_``). Old ``default_iteration`` / misspelled keys are ignored.
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


# ══════ ⑩ llm (config doc §2 ⑩) ══════


class LlmProviderSettings(BaseModel):
    """§2 llm.providers.* — direct values (trusted intranet, yaml plaintext)."""

    model_config = _C
    api_key: str = ""
    base_url: str = ""


class LlmModelSettings(BaseModel):
    model: str = ""
    temperature: Optional[float] = None


class LlmSettings(BaseModel):
    """§2 llm — platform LLM (judge/attribution/hypothesis/critic)."""

    model_config = _C
    providers: dict[str, LlmProviderSettings] = Field(default_factory=lambda: {
        "anthropic": LlmProviderSettings(api_key=""),
        "openai_compat": LlmProviderSettings(api_key="", base_url=""),
    })
    defaults: dict[str, LlmModelSettings] = Field(default_factory=lambda: {
        "judge": LlmModelSettings(model="", temperature=0.0),
        "attribution": LlmModelSettings(model=""),
        "hypothesis": LlmModelSettings(model=""),
        "critic": LlmModelSettings(model=""),
    })
    cassette_mode: Literal["replay", "record", "off"] = "replay"
    max_concurrent_calls: int = 4


# ══════ ⑪ scheduler & seed (config doc §2 ⑫) ══════


class SchedulerSettings(BaseModel):
    """§2 scheduler — P0/P1 asyncio, P2 rq (internal config)."""

    model_config = _C
    kind: Literal["asyncio", "rq"] = "asyncio"
    redis_url: Optional[str] = None


class SeedSettings(BaseModel):
    """§2 seed — global master seed for three-stage derivation."""

    model_config = _C
    master: int = 20261102


class Settings(BaseSettings):
    """Root platform settings — three-tier (env / user yaml / internal yaml)."""

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
        """Priority: init kwargs > env (RSI_*) > settings.yaml (user tier)
        > settings-internal.yaml (internal tier) > defaults.

        Both yaml files live under RSI_HOME (RSI_SETTINGS /
        RSI_INTERNAL_SETTINGS override); a missing file yields an empty
        source. Internal-tier keys found in the user settings.yaml are
        ignored; the internal file only contributes internal-tier keys.
        """
        user_yaml = _TierYamlConfigSettingsSource(
            settings_cls, yaml_file=str(settings_yaml_path()), keep_internal=False
        )
        internal_yaml = _TierYamlConfigSettingsSource(
            settings_cls,
            yaml_file=str(settings_internal_yaml_path()),
            keep_internal=True,
        )
        return (
            init_settings,
            env_settings,
            user_yaml,
            internal_yaml,
            dotenv_settings,
            file_secret_settings,
        )

    # RSI_HOME: deployment env var, read live (not a Settings field so it
    # never shows up in GET /system/settings or model_dump).
    @property
    def home(self) -> Path:
        return _rsi_home()

    def model_post_init(self, __context) -> None:
        # Ensure the derived platform directory layout exists.
        for d in (
            self.git_repo_path.parent,
            self.git_worktrees_dir,
            self.traces_dir,
            self.artifacts_dir,
            self.labs_dir,
            self.recordings_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        """Concrete DB URL (SQLite default under RSI_HOME)."""
        return self.database.url or f"sqlite:///{self.home / 'platform.db'}"

    def resolve(self, value: str | Path) -> Path:
        """Resolve a possibly-relative path against RSI_HOME."""
        return _resolve(self.home, value)

    # Derived path layout (fixed, from RSI_HOME — no longer configurable).
    @property
    def traces_dir(self) -> Path:
        return self.home / "storage" / "traces"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "storage" / "artifacts"

    @property
    def labs_dir(self) -> Path:
        return self.home / "storage" / "labs"

    @property
    def recordings_dir(self) -> Path:
        return self.home / "storage" / "recordings"

    @property
    def stop_file(self) -> Path:
        return self.home / "storage" / "STOP"

    # explicit alias (config refactor C1 naming)
    stop_file_path = stop_file

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def git_repo_path(self) -> Path:
        return self.home / "git" / "platform-repo.git"

    @property
    def git_worktrees_dir(self) -> Path:
        return self.home / "git" / "wt"
