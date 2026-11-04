"""Domain enums and state machine transition tables (DD-01 §3.11).

All enums are StrEnum. Each state machine exposes a ``VALID_*_TRANSITIONS``
dict mapping a source state to the frozenset of permitted target states.
Terminal states simply have no entry in the dict (no outgoing transitions).

Invalid transitions raise ``InvalidTransitionError`` at the call site
(TU-001/TU-027 consumers); this module only declares the tables and a
pure helper :func:`is_valid_transition`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping


class BatchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    JUDGING = "judging"
    FINALIZED = "finalized"
    FAILED = "failed"
    ABORTED = "aborted"


class RunStatus(StrEnum):
    PENDING = "pending"
    ENV_BUILDING = "env_building"
    ENV_READY = "env_ready"
    SUT_RUNNING = "sut_running"
    COLLECTING = "collecting"
    JUDGING = "judging"
    DONE = "done"
    ENV_FAILED = "env_failed"
    SUT_TIMEOUT = "sut_timeout"
    ABORTED = "aborted"
    CACHED = "cached"


class LabStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAULTED = "faulted"
    DESTROYED = "destroyed"
    ERROR = "error"


class EventType(StrEnum):
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SKILL_LOADED = "skill_loaded"
    KNOWLEDGE_QUERY = "knowledge_query"
    SCRIPT_EXEC = "script_exec"
    ERROR = "error"
    TASK_END = "task_end"


class HypothesisStatus(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    CRITIC_PASSED = "critic_passed"
    APPLIED = "applied"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class IterationState(StrEnum):
    ATTRIBUTING = "attributing"
    HYPOTHESIZING = "hypothesizing"
    REVIEWING = "reviewing"
    APPLYING = "applying"
    VERIFYING = "verifying"
    DECIDING = "deciding"
    PAUSED = "paused"
    ABORTED = "aborted"
    ARCHIVED = "archived"
    FINISHED = "finished"


class InstanceStatus(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    DESTROYED = "destroyed"
    FAILED = "failed"


class ExperimentDecision(StrEnum):
    PROMOTED = "promoted"
    REVERTED = "reverted"
    PENDING = "pending"
    ADVISE_MANUAL = "advise_manual"


# --------------------------------------------------------------------------
# Transition tables (DD-01 §3.11)
# --------------------------------------------------------------------------

VALID_BATCH_TRANSITIONS: Mapping[BatchStatus, frozenset[BatchStatus]] = {
    BatchStatus.CREATED: frozenset({BatchStatus.RUNNING}),
    BatchStatus.RUNNING: frozenset({BatchStatus.JUDGING, BatchStatus.ABORTED}),
    BatchStatus.JUDGING: frozenset(
        {BatchStatus.FINALIZED, BatchStatus.FAILED, BatchStatus.ABORTED}
    ),
    # FINALIZED / FAILED / ABORTED are terminal: no outgoing transitions.
}

VALID_RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.ENV_BUILDING, RunStatus.CACHED}),
    RunStatus.ENV_BUILDING: frozenset({RunStatus.ENV_READY, RunStatus.ENV_FAILED}),
    RunStatus.ENV_READY: frozenset({RunStatus.SUT_RUNNING, RunStatus.ABORTED}),
    RunStatus.SUT_RUNNING: frozenset(
        {RunStatus.COLLECTING, RunStatus.SUT_TIMEOUT, RunStatus.ABORTED}
    ),
    RunStatus.COLLECTING: frozenset({RunStatus.JUDGING, RunStatus.ABORTED}),
    RunStatus.JUDGING: frozenset({RunStatus.DONE}),
    # DONE / ENV_FAILED / SUT_TIMEOUT / ABORTED / CACHED are terminal.
}

VALID_LAB_TRANSITIONS: Mapping[LabStatus, frozenset[LabStatus]] = {
    LabStatus.BUILDING: frozenset({LabStatus.READY, LabStatus.FAULTED}),
    LabStatus.READY: frozenset({LabStatus.FAULTED, LabStatus.DESTROYED}),
    LabStatus.FAULTED: frozenset({LabStatus.DESTROYED, LabStatus.ERROR}),
    # DESTROYED / ERROR are terminal.
}

VALID_HYPOTHESIS_TRANSITIONS: Mapping[
    HypothesisStatus, frozenset[HypothesisStatus]
] = {
    HypothesisStatus.DRAFT: frozenset(
        {HypothesisStatus.PROPOSED, HypothesisStatus.WITHDRAWN}
    ),
    HypothesisStatus.PROPOSED: frozenset(
        {HypothesisStatus.CRITIC_PASSED, HypothesisStatus.WITHDRAWN}
    ),
    HypothesisStatus.CRITIC_PASSED: frozenset(
        {HypothesisStatus.APPLIED, HypothesisStatus.WITHDRAWN}
    ),
    HypothesisStatus.APPLIED: frozenset(
        {HypothesisStatus.ACCEPTED, HypothesisStatus.REJECTED, HypothesisStatus.WITHDRAWN}
    ),
    # ACCEPTED / REJECTED / WITHDRAWN are terminal.
}

VALID_ITERATION_TRANSITIONS: Mapping[IterationState, frozenset[IterationState]] = {
    IterationState.ATTRIBUTING: frozenset(
        {IterationState.HYPOTHESIZING, IterationState.PAUSED, IterationState.ABORTED}
    ),
    IterationState.HYPOTHESIZING: frozenset(
        {IterationState.REVIEWING, IterationState.PAUSED, IterationState.ABORTED}
    ),
    IterationState.REVIEWING: frozenset(
        {IterationState.APPLYING, IterationState.PAUSED, IterationState.ABORTED}
    ),
    IterationState.APPLYING: frozenset(
        {IterationState.VERIFYING, IterationState.PAUSED, IterationState.ABORTED}
    ),
    IterationState.VERIFYING: frozenset(
        {IterationState.DECIDING, IterationState.PAUSED, IterationState.ABORTED}
    ),
    IterationState.DECIDING: frozenset(
        {
            IterationState.ATTRIBUTING,
            IterationState.FINISHED,
            IterationState.PAUSED,
            IterationState.ABORTED,
        }
    ),
    IterationState.PAUSED: frozenset(
        {
            # PAUSED resumes to any running state; a paused iteration may also
            # be aborted by an operator.
            IterationState.ATTRIBUTING,
            IterationState.HYPOTHESIZING,
            IterationState.REVIEWING,
            IterationState.APPLYING,
            IterationState.VERIFYING,
            IterationState.DECIDING,
            IterationState.ABORTED,
        }
    ),
    IterationState.FINISHED: frozenset({IterationState.ARCHIVED}),
    IterationState.ABORTED: frozenset({IterationState.ARCHIVED}),
    # ARCHIVED is terminal.
}

VALID_INSTANCE_TRANSITIONS: Mapping[
    InstanceStatus, frozenset[InstanceStatus]
] = {
    InstanceStatus.CREATING: frozenset({InstanceStatus.RUNNING, InstanceStatus.FAILED}),
    InstanceStatus.RUNNING: frozenset(
        {InstanceStatus.STOPPED, InstanceStatus.DESTROYED, InstanceStatus.FAILED}
    ),
    InstanceStatus.STOPPED: frozenset(
        {InstanceStatus.RUNNING, InstanceStatus.DESTROYED}
    ),
    # DESTROYED / FAILED are terminal.
}


def is_valid_transition(
    enum_class: type[StrEnum],
    from_val: StrEnum | str,
    to_val: StrEnum | str,
) -> bool:
    """Return True iff ``from_val -> to_val`` is permitted by the matching table.

    Accepts enum members or raw string values. Unknown state machines raise
    ``ValueError``.
    """
    tables: Mapping[type[StrEnum], Mapping[StrEnum, frozenset[StrEnum]]] = {
        BatchStatus: VALID_BATCH_TRANSITIONS,
        RunStatus: VALID_RUN_TRANSITIONS,
        LabStatus: VALID_LAB_TRANSITIONS,
        HypothesisStatus: VALID_HYPOTHESIS_TRANSITIONS,
        IterationState: VALID_ITERATION_TRANSITIONS,
        InstanceStatus: VALID_INSTANCE_TRANSITIONS,
    }
    try:
        table = tables[enum_class]
    except KeyError:
        raise ValueError(
            f"No transition table registered for {enum_class!r}"
        ) from None
    src = enum_class(from_val)
    dst = enum_class(to_val)
    return dst in table.get(src, frozenset())
