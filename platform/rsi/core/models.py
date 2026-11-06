"""rsi.core.models: all Pydantic domain models (DD-01 §3.1-3.10).

Pure data + validation only — no IO, no network, no LLM calls. All models
are frozen, forbid extra fields, and strip leading/trailing whitespace from
strings. Cross-field constraints are enforced with ``model_validator``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rsi.core.enums import EventType, IterationState
from rsi.core.errors import NamingError, UnknownDeviceError

__all__ = [
    "SshAccess",
    "DeviceSpec",
    "LinkSpec",
    "Topology",
    "ReadyCheck",
    "ConfigApi",
    "ExternalDep",
    "EnvSpec",
    "CliEntry",
    "CallApiParams",
    "PrepareAction",
    "TaskSpec",
    "RootCauseExpectation",
    "StateAssert",
    "CheckItem",
    "RiskItem",
    "CriticalStep",
    "ExpectedAnswer",
    "MetricDef",
    "TaskRecord",
    "EvalTask",
    "TraceEvent",
    "WiretapFrame",
    "Scores",
    "PathQuality",
    "Efficiency",
    "SafetyVerdict",
    "Verdict",
    "ProblemStatement",
    "FAILURE_MODES",
    "Hypothesis",
    "AttributionCluster",
    "AttributionReport",
    "Goal",
    "EvalsetBinding",
    "GuardPolicy",
    "StopPolicy",
    "Budget",
    "LlmCompare",
    "OptimizationProfile",
    "ActionRecord",
    "TaskResult",
    "LLMUsage",
    "LLMResponse",
    "LabHandle",
    "AppliedAction",
    "SnapRef",
    "BatchStats",
    "Decision",
    "CostDelta",
    "NoiseBandInfo",
    "DecisionCtx",
    "IterationSnapshot",
]


class _M(BaseModel):
    """Shared model config: immutable, strict, whitespace-stripped."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# §3.1 Evaluation-set triplet
# ---------------------------------------------------------------------------


class SshAccess(_M):
    ip: str  # IPv4/IPv6
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    password: str | None = None  # XOR password_ref
    password_ref: str | None = None  # CredentialStore name

    @model_validator(mode="after")
    def _password_xor_ref(self) -> "SshAccess":
        if self.password is not None and self.password_ref is not None:
            raise ValueError("password and password_ref are mutually exclusive")
        return self


class DeviceSpec(_M):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    type: Literal["vrp", "ios", "ce"]  # -> netmiko device_type (DD-04)
    version: str | None = None
    config: str  # relative path under configs/, e.g. "configs/R1.cfg"


class LinkSpec(_M):
    endpoints: tuple[str, str]  # ("R1:GE0/0/1", "R2:GE0/0/1")
    type: Literal["ethernet", "serial"] = "ethernet"


class Topology(_M):
    links: list[LinkSpec] = []


class ReadyCheck(_M):
    type: Literal["ssh_reachable", "cli_match"]
    device: str | None = None  # required for cli_match
    cli: str | None = None
    expect: str | None = None  # required for cli_match

    @model_validator(mode="after")
    def _cli_match_fields(self) -> "ReadyCheck":
        if self.type == "cli_match" and (
            self.device is None or self.cli is None or self.expect is None
        ):
            raise ValueError(
                "cli_match requires device, cli and expect"
            )
        return self


class ConfigApi(_M):
    """External configuration API endpoint (ADR-20 call_api vehicle)."""

    base_url: str  # http(s)://...
    auth_ref: str | None = None  # CredentialStore name; None = no auth
    timeout_s: int = 60


class ExternalDep(_M):
    """P2 NMS endpoint declaration."""

    kind: Literal["nms"]
    base_url: str
    auth_ref: str | None = None


class EnvSpec(_M):
    name: str
    backend: Literal["ensp-pro", "pnetlab", "containerlab", "fake"]
    devices: list[DeviceSpec] = Field(min_length=1)
    topology: Topology
    ssh_access: dict[str, SshAccess]  # keys == device names
    ready_checks: list[ReadyCheck] = []  # default: all devices ssh_reachable
    config_apis: dict[str, ConfigApi] = {}
    external_deps: list[ExternalDep] = []

    @model_validator(mode="after")
    def _devices_and_ssh_keys(self) -> "EnvSpec":
        names = [d.name for d in self.devices]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate device names in {names!r}")
        keys = set(self.ssh_access)
        dev = set(names)
        if keys != dev:
            missing = sorted(dev - keys)
            extra = sorted(keys - dev)
            raise ValueError(
                f"ssh_access keys must equal device names; missing={missing}, extra={extra}"
            )
        return self

    def apply_site_override(self, site: "EnvSpec") -> "EnvSpec":
        """Merge a site-level EnvSpec; only backend and ssh_access may differ.

        Any other field mismatch raises ``NamingError`` (SiteOverrideError
        semantics per DD-01 §3.12).
        """
        self_fields = self.model_dump(exclude={"backend", "ssh_access"})
        site_fields = site.model_dump(exclude={"backend", "ssh_access"})
        differing = sorted(
            k for k in self_fields if self_fields[k] != site_fields.get(k)
        )
        if differing:
            raise NamingError(
                "site override may only change backend and ssh_access",
                context={"differing_fields": differing},
            )
        return self.model_copy(
            update={"backend": site.backend, "ssh_access": dict(site.ssh_access)}
        )


class CliEntry(_M):
    """Single-device exec_cli entry."""

    device: str
    cli_lines: list[str] = Field(min_length=1)


class CallApiParams(_M):
    """call_api parameters (ADR-20)."""

    env_name: str
    scenario: str
    devices: list[CliEntry] = Field(min_length=1)


class PrepareAction(_M):
    action: Literal["exec_cli", "call_api"]
    entries: list[CliEntry] | None = None  # required for exec_cli
    api: str | None = None  # required for call_api: env.config_apis key
    params: CallApiParams | None = None  # required for call_api

    @model_validator(mode="after")
    def _action_fields(self) -> "PrepareAction":
        if self.action == "exec_cli":
            if self.entries is None:
                raise ValueError("exec_cli requires entries")
            if self.api is not None or self.params is not None:
                raise ValueError("exec_cli forbids api/params")
        else:  # call_api
            if self.api is None or self.params is None:
                raise ValueError("call_api requires api and params")
            if self.entries is not None:
                raise ValueError("call_api forbids entries")
        return self


class TaskSpec(_M):
    prompt: str  # includes FINAL_JSON convention text
    context: dict[str, Any] = {}
    tool_constraints: list[str] = []


class RootCauseExpectation(_M):
    """ADR-23: natural-language expectation + optional hints."""

    expected: str  # NL root-cause description (LLMJudge semantic compare)
    match_hints: list[str] = []  # highlighting/prefilter only, not criteria


class StateAssert(_M):
    device: str
    cli: str  # read-only command (prefix whitelist checked in sandbox)
    expect_contains: list[str] = []  # multi-value AND
    expect_absent: list[str] = []

    @model_validator(mode="after")
    def _at_least_one_expect(self) -> "StateAssert":
        if not self.expect_contains and not self.expect_absent:
            raise ValueError(
                "StateAssert needs at least one of expect_contains/expect_absent"
            )
        return self


class CheckItem(_M):
    item: str
    evidence_cli: str | None = None


class RiskItem(_M):
    risk: str
    severity: Literal["high", "medium", "low"] = "medium"


class CriticalStep(_M):
    evidence: str  # expected command fragment (tool_call string match)


class ExpectedAnswer(_M):
    root_cause: RootCauseExpectation | None = None
    final_state: list[StateAssert] = []
    checklist: list[CheckItem] | None = None
    risk_findings: list[RiskItem] | None = None
    critical_steps: list[CriticalStep] | None = None


class MetricDef(_M):
    name: Literal[
        "root_cause_accuracy",
        "task_complete",
        "diagnostic_steps",
        "missed_check",
        "false_positive",
        "tokens",
        "tool_calls",
        "wall_time_s",
    ]


class TaskRecord(_M):
    """One tasks.jsonl line."""

    task_id: str = Field(pattern=r"^(fault|inspection|cutover)/[a-z0-9-]+-\d{2}$")
    title: str
    prepare: list[PrepareAction] = []
    task: TaskSpec
    expected: ExpectedAnswer
    metrics: list[MetricDef] = []


# ---------------------------------------------------------------------------
# §3.2 Runtime pairing and trace
# ---------------------------------------------------------------------------


class EvalTask(_M):
    """Runtime pairing: EnvSpec x TaskRecord."""

    env: EnvSpec
    task: TaskRecord

    def resolve_device(self, name: str) -> SshAccess:
        try:
            return self.env.ssh_access[name]
        except KeyError:
            raise UnknownDeviceError(
                f"device {name!r} not present in env {self.env.name!r}",
                context={"device": name, "env": self.env.name},
            ) from None


class TraceEvent(_M):
    run_id: str
    task_id: str
    lab_id: str
    sut_id: str  # plain identifier (version resolved via run)
    seq: int = Field(ge=0)
    ts: str  # UTC ISO8601 with milliseconds
    event_type: EventType
    model: str | None = None
    data: dict[str, Any] = {}
    duration_ms: int | None = None
    session_id: str | None = None
    step_seq: int | None = None
    late: bool = False  # arrived after finalize grace (v2.3 T3)


class WiretapFrame(_M):
    """Carrier-native frame; parser input (DD-07)."""

    carrier: Literal["opencode", "claude_code"]
    kind: str  # opencode: assembled/http; cc: system/assistant/user/result
    raw: dict[str, Any]
    at: str


# ---------------------------------------------------------------------------
# §3.3 Judgment
# ---------------------------------------------------------------------------


class Scores(_M):
    root_cause_accuracy: float = Field(ge=0, le=1)
    task_complete: bool
    diagnostic_steps: int = Field(ge=0)
    missed_check: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    indeterminate: bool = False


class PathQuality(_M):
    verdict: Literal["pass", "suspect"]
    skipped_critical_steps: list[str] = []
    reasoning: str


class Efficiency(_M):
    tokens: int = 0
    tool_calls: int = 0
    wall_time_s: float = 0.0
    estimated: bool = False  # True when tokens estimated from char count (C.5(6))


class SafetyVerdict(_M):
    violations: list[str] = []
    fatal: bool = False
    major: int = 0
    minor: int = 0


class Verdict(_M):
    scores: Scores
    path_quality: PathQuality
    efficiency: Efficiency
    safety: SafetyVerdict
    reward_hacking_suspect: bool = False
    missing: bool = False  # missing run kept in denominator (aggregation layer)


# ---------------------------------------------------------------------------
# §3.4 Hypothesis and attribution
# ---------------------------------------------------------------------------


class ProblemStatement(_M):
    """DGM problem ticket (FR6.3 stage1)."""

    cluster_id: str
    failure_mode: str
    defect_class: Literal["A", "B"]
    evidence_trace_ids: list[str]  # run_id references
    evidence_sut_refs: list[str] = []  # "suts/<id>/<path>#L<line>" (v2.10)
    suspected_elements: list[str]  # file paths
    problem_text: str


FAILURE_MODES = Literal[
    "call_order_error",
    "missed_critical_command",
    "reasoning_skip",
    "causal_misattribution",
    "hallucinated_command",
    "alert_not_condensed",
    "yang_path_error",
    "knowledge_missing",
    "output_unparseable",
    "other",
]


class Hypothesis(_M):
    problem_trace_ids: list[str]
    failure_type: FAILURE_MODES | str
    defect_class: Literal["A", "B"]
    proposal_type: Literal[
        "modify_prompt",
        "add_runbook",
        "adjust_tool_sequence",
        "add_fewshot_example",
        "add_knowledge",
        "switch_model",
        "custom",
    ]
    diff_content: str | None = None  # XOR model_config_patch
    model_config_patch: dict[str, Any] | None = None
    expected_improve: str
    risk: Literal["low", "medium", "high"]
    verification_task_ids: list[str]
    predicted_affected_tasks: list[str]

    @model_validator(mode="after")
    def _diff_xor_patch(self) -> "Hypothesis":
        if (self.diff_content is None) == (self.model_config_patch is None):
            raise ValueError(
                "exactly one of diff_content / model_config_patch is required"
            )
        return self


class AttributionCluster(_M):
    failure_mode: str
    defect_class: Literal["A", "B"]
    cluster_size: int
    sample_run_ids: list[str]
    evidence: list[str]  # trace refs + SUT source refs (v2.10)


class AttributionReport(_M):
    clusters: list[AttributionCluster]  # sorted by size x fixability desc


# ---------------------------------------------------------------------------
# §3.5 OptimizationProfile
# ---------------------------------------------------------------------------


class Goal(_M):
    metric: str = Field(json_schema_extra={"example": "task_success_rate"})
    direction: Literal["max", "min"] = Field(json_schema_extra={"example": "max"})
    weight: float = Field(gt=0, json_schema_extra={"example": 1.0})


class EvalsetBinding(_M):
    iterate: str = Field(json_schema_extra={"example": "es-abc12345"})
    holdout: str = Field(json_schema_extra={"example": "es-def67890"})
    smoke: str = Field(json_schema_extra={"example": "es-aaa11111"})


class GuardPolicy(_M):
    """Non-compensatory guard switches (DD-10 §5.1 rule 2).

    ``reject`` (default): the guard is enforced; ``off``: disabled (D-7).
    """

    missed_check_worsen: Literal["reject", "off"] = "reject"
    safety_any_worsen: Literal["reject", "off"] = "reject"
    high_risk_worsen: Literal["reject", "off"] = "reject"


class StopPolicy(_M):
    max_rounds: int = Field(default=20, ge=1, json_schema_extra={"example": 20})
    no_gain_rounds: int = Field(default=5, ge=1, json_schema_extra={"example": 5})


class Budget(_M):
    max_tokens: int = Field(gt=0, json_schema_extra={"example": 1000000})
    max_wall_hours: float = Field(gt=0, json_schema_extra={"example": 8.0})
    max_cost_usd: float | None = Field(default=None, json_schema_extra={"example": 50.0})


class LlmCompare(_M):
    candidates: list[str] = Field(default=[], json_schema_extra={"example": ["glm-4.7", "glm-4.6"]})


class OptimizationProfile(_M):
    name: str = Field(json_schema_extra={"example": "bgp-triage-tuning"})
    evalsets: EvalsetBinding
    goals: list[Goal] = Field(min_length=1)
    enabled_targets: set[
        Literal[
            "system_prompt",
            "skills",
            "scripts",
            "mcp_config",
            "mcp_src",
            "memory",
            "knowledge",
            "model_config",
        ]
    ] = Field(
        min_length=1,
        json_schema_extra={"example": ["system_prompt", "skills", "memory"]},
    )
    guards: GuardPolicy = GuardPolicy()
    stop: StopPolicy = StopPolicy()
    budget: Budget | None = None  # None = orchestrator.budget default
    llm_compare: LlmCompare | None = None


# ---------------------------------------------------------------------------
# §3.6 SUT task result (Q6)
# ---------------------------------------------------------------------------


class ActionRecord(_M):
    action: str  # e.g. "display bgp peer"
    detail: str | None = None


class TaskResult(_M):
    task_complete: bool
    root_cause: str | None = None
    actions: list[ActionRecord] = []
    confidence: float | None = None
    parse_error: bool = False  # FINAL_JSON extraction failed (class-A sample)
    instance_crashed: bool = False  # F2: container non-zero exit (infra fault)


# ---------------------------------------------------------------------------
# §3.7 LLM layer (DD-02 consumer)
# ---------------------------------------------------------------------------


class LLMUsage(_M):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False


class LLMResponse(_M):
    content: str  # raw text
    parsed: Any | None = None  # schema-validated structured output
    usage: LLMUsage = LLMUsage()
    model: str
    prompt_version: str
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# §3.8 Sandbox handles and applied actions
# ---------------------------------------------------------------------------


class LabHandle(_M):
    lab_id: str
    host: str
    backend: str
    endpoints: dict[str, SshAccess]  # verified endpoints (ADR-24 3-level resolution)


class AppliedAction(_M):
    """Prepare execution record (restore_task input / ops.jsonl line)."""

    action: Literal["exec_cli", "call_api"]
    entries: list[CliEntry] | None = None
    api: str | None = None
    params: CallApiParams | None = None
    backup_files: dict[str, str] = {}  # device -> backups/<task>/<dev>.cfg path
    applied_at: str


class SnapRef(_M):
    snapshot_id: str
    impl_kind: Literal["native", "degraded"]
    backend_ref: str | None = None


# ---------------------------------------------------------------------------
# §3.9 Decision (DD-10 consumer)
# ---------------------------------------------------------------------------


class BatchStats(_M):
    metrics: dict[str, float]  # metric -> mean
    n: int
    missing: int  # missing runs kept in denominator (rule 0)
    tokens: int
    tool_calls: int
    wall_time_s: float
    tokens_estimated: bool
    # D-9: per-task scores for paired significance + hit-rate recalc.
    # Entry shape: {task_id, score, task_complete, missing, ...} (list keeps
    # empty default so existing constructors are unaffected).
    task_scores: list[dict[str, Any]] = []


class Decision(_M):
    action: Literal["PROMOTE", "REVERT", "REJECT", "ADVISE_MANUAL"]
    rule_hit: int  # rule index 0-6
    reason: str
    evidence: dict[str, Any]  # batch ids / metric deltas / test results


class CostDelta(_M):
    tokens: int
    tool_calls: int
    wall_time_s: float
    cost_usd: float | None = None


class NoiseBandInfo(_M):
    """Noise band calibration result (D-8, aligned with DD-10 §3 / §9.17).

    ``metrics`` maps metric name -> {sigma, delta, n_pairs}; the band is
    valid only when enough paired tasks survived calibration.
    """

    metrics: dict[str, dict[str, float]] = {}
    k: int = 0  # calibration repeat count (ADR-17)
    evalset_hash: str = ""
    low_confidence: bool = False  # valid tasks < sampled / 2
    valid: bool = False  # not low_confidence and n_pairs >= 2
    n_pairs: int = 0  # legacy convenience: min per-metric pair count


class DecisionCtx(_M):
    """RegularizedSelector input (DD-10 §3 / interface §12)."""

    base_stats: BatchStats
    cand_stats: BatchStats
    noise_delta: float | None = None  # uncalibrated -> rules degrade with delta 0
    goals: list[Goal] = Field(min_length=1)
    guards: GuardPolicy = GuardPolicy()
    cost: dict[str, Any] = {}  # e.g. {"cost_penalty": 0.1, "tokens": 1200, ...}
    hack_or_fatal: bool = False
    # D-7: per-metric noise sigma (metric -> sigma); when present the
    # weighted composite score normalizes each delta by its sigma (§5.2).
    metric_noise: dict[str, float] | None = None
    # D-7: paired per-task primary scores for the significance test (rule 5).
    base_task_scores: list[float] | None = None
    cand_task_scores: list[float] | None = None


# ---------------------------------------------------------------------------
# §3.10 Iteration and gate
# ---------------------------------------------------------------------------


class IterationSnapshot(_M):
    """GET /iterations/{id} response body."""

    id: str
    sut_id: str
    mode: Literal["p1", "p2"]
    state: IterationState
    current_step: str
    round: int
    experiments: list[str]
    budget_used: dict[str, Any]  # {tokens, cost_usd, wall_time_s}
