"""SQLModel table classes for the RSI Playground metadata database.

Schema source: docs/_rsi_playground_interfaces.md §2.2 (authoritative DDL).
Conventions: plural table names, TEXT ids (UUID strings), UTC ISO8601
timestamps stored as TEXT, INTEGER for booleans, JSON payloads as TEXT
(serialized by the service layer). These classes are independent of the
domain models in ``rsi.core.models``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class OptimizationProfile(SQLModel, table=True):
    """FR6.1 optimization configuration profile (config doc §3)."""

    __tablename__ = "optimization_profiles"
    __table_args__ = (
        UniqueConstraint("sut_id", "name", name="uq_optimization_profiles_sut_name"),
    )

    id: str = Field(primary_key=True)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    name: str = Field(nullable=False)
    evalsets_json: str = Field(nullable=False)  # {iterate, holdout, smoke}
    goals_json: str = Field(nullable=False)  # [{metric, direction, weight}]
    enabled_targets_json: str = Field(nullable=False)  # ["system_prompt", ...]
    guards_json: str = Field(nullable=False)
    stop_json: str = Field(nullable=False)
    budget_json: str = Field(nullable=False)
    llm_compare_json: Optional[str] = None
    created_at: str = Field(nullable=False)


class Iteration(SQLModel, table=True):
    """Optimization iteration (FR6.1/ADR-21)."""

    __tablename__ = "iterations"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('p1','p2')", name="ck_iterations_mode"
        ),
    )

    id: str = Field(primary_key=True)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    profile_id: str = Field(
        foreign_key="optimization_profiles.id", nullable=False
    )
    mode: str = Field(nullable=False)  # p1 | p2
    state: str = Field(nullable=False)
    round: int = Field(nullable=False, default=0)
    created_at: str = Field(nullable=False)
    finished_at: Optional[str] = None
    # D-4 (设计 §5.4): PAUSED 落库 — 人工门 pause 记录列（等待步骤+时间戳）。
    # 可空：未 PAUSED 或旧模式（内存 store）的行保持 NULL。
    paused_step: Optional[str] = None
    paused_at: Optional[str] = None


class User(SQLModel, table=True):
    """User account (NFR8)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin','agent_engineer','researcher','approver','service')",
            name="ck_users_role",
        ),
    )

    id: str = Field(primary_key=True)
    username: str = Field(unique=True, nullable=False)
    role: str = Field(nullable=False)
    password_hash: str = Field(nullable=False)
    disabled: int = Field(default=0)
    created_at: str = Field(nullable=False)


class Credential(SQLModel, table=True):
    """FR1.5 credential storage (Fernet-encrypted blob)."""

    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('device_ssh','nms_api','llm_api','backend_ssh')",
            name="ck_credentials_kind",
        ),
    )

    id: str = Field(primary_key=True)
    name: str = Field(unique=True, nullable=False)
    kind: str = Field(nullable=False)
    cipher_blob: bytes = Field(nullable=False)
    created_at: str = Field(nullable=False)


class AuditLogEntry(SQLModel, table=True):
    """Audit trail (NFR1-4)."""

    __tablename__ = "audit_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str = Field(nullable=False, index=True)
    actor: str = Field(nullable=False)
    action: str = Field(nullable=False)
    object_type: str = Field(nullable=False)
    object_id: str = Field(nullable=False)
    detail_json: Optional[str] = None


class Sut(SQLModel, table=True):
    """System under test (FR1/FR8)."""

    __tablename__ = "suts"
    __table_args__ = (
        CheckConstraint(
            "carrier IN ('claude_code','opencode','hermes','custom')",
            name="ck_suts_carrier",
        ),
    )

    id: str = Field(primary_key=True)
    name: str = Field(unique=True, nullable=False)
    carrier: str = Field(nullable=False)
    image_ref: str = Field(nullable=False)
    description: str = Field(default="")
    baseline_version_id: Optional[str] = None  # config_versions.id
    created_at: str = Field(nullable=False)


class ConfigVersion(SQLModel, table=True):
    """SUT configuration version (FR1/FR8)."""

    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('baseline','candidate','archive')",
            name="ck_config_versions_kind",
        ),
    )

    id: str = Field(primary_key=True)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    git_ref: str = Field(nullable=False)
    label: str = Field(nullable=False)
    kind: str = Field(nullable=False)
    parent_id: Optional[str] = Field(default=None, foreign_key="config_versions.id")
    created_at: str = Field(nullable=False)
    created_by: str = Field(nullable=False)


class EvalSet(SQLModel, table=True):
    """Evaluation set (FR3)."""

    __tablename__ = "evalsets"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('iterate','holdout','generalization','smoke')",
            name="ck_evalsets_purpose",
        ),
    )

    id: str = Field(primary_key=True)
    name: str = Field(unique=True, nullable=False)
    purpose: str = Field(nullable=False)
    git_ref: Optional[str] = None
    content_hash: Optional[str] = None  # filled on freeze (FR3.3)
    frozen_at: Optional[str] = None
    created_at: str = Field(nullable=False)


class EvalTask(SQLModel, table=True):
    """Evaluation set task (rows of tasks.jsonl)."""

    __tablename__ = "eval_tasks"
    __table_args__ = (
        UniqueConstraint("evalset_id", "task_id", name="uq_eval_tasks_set_task"),
    )

    id: str = Field(primary_key=True)
    evalset_id: str = Field(foreign_key="evalsets.id", nullable=False)
    task_id: str = Field(nullable=False)
    title: str = Field(nullable=False)
    source_ref: str = Field(nullable=False)
    content_hash: str = Field(nullable=False)  # ADR-5 cache key component


class Lab(SQLModel, table=True):
    """Experiment environment (FR4)."""

    __tablename__ = "labs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building','ready','faulted','destroyed','error')",
            name="ck_labs_status",
        ),
    )

    id: str = Field(primary_key=True)
    host: str = Field(nullable=False)
    backend: str = Field(nullable=False)
    env_hash: str = Field(nullable=False)
    status: str = Field(nullable=False)
    online: int = Field(default=1)
    devices_online: Optional[str] = None  # "2/3" (v2.17 H2)
    device_endpoints_json: str = Field(nullable=False)  # ADR-24 authoritative
    created_at: str = Field(nullable=False)
    destroyed_at: Optional[str] = None


class LabSnapshot(SQLModel, table=True):
    """Lab snapshot record (FR4)."""

    __tablename__ = "lab_snapshots"
    __table_args__ = (
        CheckConstraint(
            "impl_kind IN ('native','degraded')", name="ck_lab_snapshots_impl_kind"
        ),
    )

    id: str = Field(primary_key=True)
    lab_id: str = Field(foreign_key="labs.id", nullable=False)
    name: str = Field(nullable=False)
    impl_kind: str = Field(nullable=False)
    backend_ref: Optional[str] = None
    created_at: str = Field(nullable=False)


class Batch(SQLModel, table=True):
    """Evaluation batch (FR5)."""

    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('baseline_eval','candidate_eval','smoke','iterate_screen',"
            "'model_compare','noise_calibration')",
            name="ck_batches_purpose",
        ),
        CheckConstraint(
            "permission IN ('read-only','read-write')",
            name="ck_batches_permission",
        ),
    )

    id: str = Field(primary_key=True)
    evalset_id: str = Field(foreign_key="evalsets.id", nullable=False)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    config_version_id: str = Field(foreign_key="config_versions.id", nullable=False)
    purpose: str = Field(nullable=False)
    optimization_profile_id: Optional[str] = None
    status: str = Field(nullable=False)
    iteration_id: Optional[str] = Field(
        default=None, foreign_key="iterations.id"
    )
    instrument: int = Field(default=0)
    permission: str = Field(default="read-only", nullable=False)
    stats_json: Optional[str] = None
    created_at: str = Field(nullable=False)
    finished_at: Optional[str] = None


class Run(SQLModel, table=True):
    """Single task execution within a batch (FR5)."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','env_building','env_ready','sut_running',"
            "'collecting','judging','done','env_failed','sut_timeout',"
            "'aborted','cached')",
            name="ck_runs_status",
        ),
    )

    id: str = Field(primary_key=True)
    batch_id: str = Field(foreign_key="batches.id", nullable=False, index=True)
    eval_task_id: str = Field(foreign_key="eval_tasks.id", nullable=False)
    lab_id: Optional[str] = Field(default=None, foreign_key="labs.id")
    exec_host: Optional[str] = None
    instance_id: Optional[str] = None
    status: str = Field(nullable=False)
    cache_key: Optional[str] = Field(default=None, index=True)  # ADR-5
    cached_from: Optional[str] = Field(default=None, foreign_key="runs.id")
    result_summary_json: Optional[str] = None
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    seq_in_batch: int = Field(nullable=False)


class InstrumentationReport(SQLModel, table=True):
    """Batch instrumentation coverage report (I-8)."""

    __tablename__ = "instrumentation_reports"

    id: str = Field(primary_key=True)
    batch_id: str = Field(foreign_key="batches.id", nullable=False)
    plan_json: str = Field(nullable=False)
    coverage_json: str = Field(nullable=False)
    created_at: str = Field(nullable=False)


class TraceFile(SQLModel, table=True):
    """Trace file index; NDJSON body lives on the filesystem (FR2.4)."""

    __tablename__ = "trace_files"

    run_id: str = Field(primary_key=True, foreign_key="runs.id")
    ndjson_path: str = Field(nullable=False)
    event_count: int = Field(nullable=False)
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    bytes: int = Field(nullable=False)


class TraceEventIndex(SQLModel, table=True):
    """High-frequency filter dimensions for trace events (FR2.5 P1)."""

    __tablename__ = "trace_events_index"

    run_id: str = Field(primary_key=True)
    seq: int = Field(primary_key=True)
    event_type: str = Field(nullable=False)
    ts: str = Field(nullable=False)
    tool_name: Optional[str] = None
    skill_name: Optional[str] = None
    is_error: int = Field(default=0)


class Judgment(SQLModel, table=True):
    """Run judgment (FR7): three-layer raw outputs + aggregated verdict."""

    __tablename__ = "judgments"

    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="runs.id", nullable=False)
    det_json: Optional[str] = None
    safety_json: Optional[str] = None
    llm_json: Optional[str] = None
    verdict_json: str = Field(nullable=False)  # §2.1 Verdict schema
    judge_prompt_hash: Optional[str] = None  # FR7.6 freeze audit
    # audit3 N2: eval provenance (config_version_id / batch_purpose) kept for
    # traceability — deliberately NOT shown to the judge LLM (blind eval).
    eval_context_json: Optional[str] = None
    created_at: str = Field(nullable=False)


class Hypothesis(SQLModel, table=True):
    """Optimization hypothesis (FR6)."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','proposed','critic_passed','applied',"
            "'accepted','rejected','withdrawn')",
            name="ck_hypotheses_status",
        ),
    )

    id: str = Field(primary_key=True)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    iteration_id: Optional[str] = Field(
        default=None, foreign_key="iterations.id"
    )
    hypothesis_json: str = Field(nullable=False)  # FR6.3 schema
    status: str = Field(nullable=False)
    critic_json: Optional[str] = None
    predicted_hit_rate: Optional[float] = None
    failure_reason: Optional[str] = None
    created_at: str = Field(nullable=False)


class Experiment(SQLModel, table=True):
    """Experiment record (FR8.4)."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('promoted','reverted','pending','advise_manual')",
            name="ck_experiments_decision",
        ),
    )

    id: str = Field(primary_key=True)
    sut_id: str = Field(foreign_key="suts.id", nullable=False)
    hypothesis_id: Optional[str] = Field(
        default=None, foreign_key="hypotheses.id"
    )
    baseline_version_id: str = Field(nullable=False)
    candidate_version_id: str = Field(nullable=False)
    baseline_batch_id: Optional[str] = Field(
        default=None, foreign_key="batches.id"
    )
    candidate_batch_id: Optional[str] = Field(
        default=None, foreign_key="batches.id"
    )
    decision: Optional[str] = None
    decision_reason: Optional[str] = None
    evidence_json: Optional[str] = None
    created_at: str = Field(nullable=False)


class NoiseBand(SQLModel, table=True):
    """Noise band calibration (FR9, ADR-17)."""

    __tablename__ = "noise_bands"
    __table_args__ = (
        UniqueConstraint(
            "evalset_id", "evalset_hash", "metric",
            name="uq_noise_bands_set_hash_metric",
        ),
    )

    id: str = Field(primary_key=True)
    evalset_id: str = Field(foreign_key="evalsets.id", nullable=False)
    evalset_hash: str = Field(nullable=False)
    metric: str = Field(nullable=False)
    sigma: float = Field(nullable=False)
    delta: float = Field(nullable=False)
    k: int = Field(nullable=False)
    low_confidence: int = Field(default=0)
    calibrated_at: str = Field(nullable=False)


class LlmCall(SQLModel, table=True):
    """Platform LLM accounting (budget circuit-breaker source, FR5.6)."""

    __tablename__ = "llm_calls"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str = Field(nullable=False)
    component: str = Field(nullable=False)  # attribution/hypothesis/critic/...
    model: str = Field(nullable=False)
    prompt_version: str = Field(nullable=False)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    run_id: Optional[str] = None
    batch_id: Optional[str] = Field(default=None, index=True)


class Checkpoint(SQLModel, table=True):
    """Resume-from-checkpoint state (FR5.5)."""

    __tablename__ = "checkpoints"

    scope: str = Field(primary_key=True)  # batch_id | iteration_id
    state_json: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


# §2.2 index requirements not covered by Field(index=True):
Index(
    "ix_trace_events_index_event_type_run_id",
    TraceEventIndex.__table__.c.event_type,
    TraceEventIndex.__table__.c.run_id,
)

ALL_TABLES: tuple[type[SQLModel]] = (
    OptimizationProfile,
    Iteration,
    User,
    Credential,
    AuditLogEntry,
    Sut,
    ConfigVersion,
    EvalSet,
    EvalTask,
    Lab,
    LabSnapshot,
    Batch,
    Run,
    InstrumentationReport,
    TraceFile,
    TraceEventIndex,
    Judgment,
    Hypothesis,
    Experiment,
    NoiseBand,
    LlmCall,
    Checkpoint,
)
