import pytest

from rsi.core.enums import (
    BatchStatus,
    EventType,
    ExperimentDecision,
    HypothesisStatus,
    InstanceStatus,
    IterationState,
    LabStatus,
    RunStatus,
    VALID_BATCH_TRANSITIONS,
    VALID_HYPOTHESIS_TRANSITIONS,
    VALID_INSTANCE_TRANSITIONS,
    VALID_ITERATION_TRANSITIONS,
    VALID_LAB_TRANSITIONS,
    VALID_RUN_TRANSITIONS,
    is_valid_transition,
)


class TestEnumValues:
    def test_batch_status_values(self):
        assert BatchStatus.CREATED == "created"
        assert BatchStatus.RUNNING == "running"
        assert BatchStatus.JUDGING == "judging"
        assert BatchStatus.FINALIZED == "finalized"
        assert BatchStatus.FAILED == "failed"
        assert BatchStatus.ABORTED == "aborted"

    def test_run_status_values(self):
        expected = {
            "PENDING": "pending",
            "ENV_BUILDING": "env_building",
            "ENV_READY": "env_ready",
            "SUT_RUNNING": "sut_running",
            "COLLECTING": "collecting",
            "JUDGING": "judging",
            "DONE": "done",
            "ENV_FAILED": "env_failed",
            "SUT_TIMEOUT": "sut_timeout",
            "ABORTED": "aborted",
            "CACHED": "cached",
        }
        for name, value in expected.items():
            assert getattr(RunStatus, name) == value

    def test_lab_status_values(self):
        assert LabStatus.BUILDING == "building"
        assert LabStatus.READY == "ready"
        assert LabStatus.FAULTED == "faulted"
        assert LabStatus.DESTROYED == "destroyed"
        assert LabStatus.ERROR == "error"

    def test_event_type_values(self):
        expected = {
            "LLM_REQUEST": "llm_request",
            "LLM_RESPONSE": "llm_response",
            "TOOL_CALL": "tool_call",
            "TOOL_RESULT": "tool_result",
            "SKILL_LOADED": "skill_loaded",
            "KNOWLEDGE_QUERY": "knowledge_query",
            "SCRIPT_EXEC": "script_exec",
            "ERROR": "error",
            "TASK_END": "task_end",
        }
        for name, value in expected.items():
            assert getattr(EventType, name) == value

    def test_hypothesis_status_values(self):
        expected = {
            "DRAFT": "draft",
            "PROPOSED": "proposed",
            "CRITIC_PASSED": "critic_passed",
            "APPLIED": "applied",
            "ACCEPTED": "accepted",
            "REJECTED": "rejected",
            "WITHDRAWN": "withdrawn",
        }
        for name, value in expected.items():
            assert getattr(HypothesisStatus, name) == value

    def test_iteration_state_values(self):
        expected = {
            "ATTRIBUTING": "attributing",
            "HYPOTHESIZING": "hypothesizing",
            "REVIEWING": "reviewing",
            "APPLYING": "applying",
            "VERIFYING": "verifying",
            "DECIDING": "deciding",
            "PAUSED": "paused",
            "ABORTED": "aborted",
            "ARCHIVED": "archived",
            "FINISHED": "finished",
        }
        for name, value in expected.items():
            assert getattr(IterationState, name) == value

    def test_instance_status_values(self):
        expected = {
            "CREATING": "creating",
            "RUNNING": "running",
            "STOPPED": "stopped",
            "DESTROYED": "destroyed",
            "FAILED": "failed",
        }
        for name, value in expected.items():
            assert getattr(InstanceStatus, name) == value

    def test_experiment_decision_values(self):
        assert ExperimentDecision.PROMOTED == "promoted"
        assert ExperimentDecision.REVERTED == "reverted"
        assert ExperimentDecision.PENDING == "pending"
        assert ExperimentDecision.ADVISE_MANUAL == "advise_manual"

    def test_all_are_strenums(self):
        from enum import StrEnum

        for cls in (
            BatchStatus,
            RunStatus,
            LabStatus,
            EventType,
            HypothesisStatus,
            IterationState,
            InstanceStatus,
            ExperimentDecision,
        ):
            assert issubclass(cls, StrEnum)


class TestBatchTransitions:
    def test_created_to_running(self):
        assert BatchStatus.RUNNING in VALID_BATCH_TRANSITIONS[BatchStatus.CREATED]

    def test_running_to_judging(self):
        assert BatchStatus.JUDGING in VALID_BATCH_TRANSITIONS[BatchStatus.RUNNING]

    def test_judging_to_finalized(self):
        assert BatchStatus.FINALIZED in VALID_BATCH_TRANSITIONS[BatchStatus.JUDGING]

    def test_judging_to_failed(self):
        assert BatchStatus.FAILED in VALID_BATCH_TRANSITIONS[BatchStatus.JUDGING]

    def test_running_to_aborted(self):
        assert BatchStatus.ABORTED in VALID_BATCH_TRANSITIONS[BatchStatus.RUNNING]

    def test_judging_to_aborted(self):
        assert BatchStatus.ABORTED in VALID_BATCH_TRANSITIONS[BatchStatus.JUDGING]

    def test_finalized_is_terminal(self):
        assert BatchStatus.FINALIZED not in VALID_BATCH_TRANSITIONS  # no outgoing

    def test_failed_is_terminal(self):
        assert BatchStatus.FAILED not in VALID_BATCH_TRANSITIONS

    def test_aborted_is_terminal(self):
        assert BatchStatus.ABORTED not in VALID_BATCH_TRANSITIONS

    def test_created_cannot_jump_to_judging(self):
        assert BatchStatus.JUDGING not in VALID_BATCH_TRANSITIONS.get(
            BatchStatus.CREATED, set()
        )


class TestRunTransitions:
    def test_full_happy_path(self):
        path = [
            RunStatus.PENDING,
            RunStatus.ENV_BUILDING,
            RunStatus.ENV_READY,
            RunStatus.SUT_RUNNING,
            RunStatus.COLLECTING,
            RunStatus.JUDGING,
            RunStatus.DONE,
        ]
        for i in range(len(path) - 1):
            assert path[i + 1] in VALID_RUN_TRANSITIONS[path[i]]

    def test_env_building_to_env_failed(self):
        assert RunStatus.ENV_FAILED in VALID_RUN_TRANSITIONS[RunStatus.ENV_BUILDING]

    def test_sut_running_to_timeout(self):
        assert RunStatus.SUT_TIMEOUT in VALID_RUN_TRANSITIONS[RunStatus.SUT_RUNNING]

    def test_sut_running_to_aborted(self):
        assert RunStatus.ABORTED in VALID_RUN_TRANSITIONS[RunStatus.SUT_RUNNING]

    def test_pending_to_cached(self):
        assert RunStatus.CACHED in VALID_RUN_TRANSITIONS[RunStatus.PENDING]

    def test_done_is_terminal(self):
        assert RunStatus.DONE not in VALID_RUN_TRANSITIONS

    def test_env_failed_is_terminal(self):
        assert RunStatus.ENV_FAILED not in VALID_RUN_TRANSITIONS

    def test_terminal_states_have_no_outgoing(self):
        for terminal in (
            RunStatus.DONE,
            RunStatus.ENV_FAILED,
            RunStatus.SUT_TIMEOUT,
            RunStatus.ABORTED,
            RunStatus.CACHED,
        ):
            assert terminal not in VALID_RUN_TRANSITIONS


class TestLabTransitions:
    def test_building_to_ready(self):
        assert LabStatus.READY in VALID_LAB_TRANSITIONS[LabStatus.BUILDING]

    def test_building_to_faulted(self):
        assert LabStatus.FAULTED in VALID_LAB_TRANSITIONS[LabStatus.BUILDING]

    def test_ready_to_destroyed(self):
        assert LabStatus.DESTROYED in VALID_LAB_TRANSITIONS[LabStatus.READY]

    def test_ready_to_faulted(self):
        assert LabStatus.FAULTED in VALID_LAB_TRANSITIONS[LabStatus.READY]

    def test_faulted_to_destroyed(self):
        assert LabStatus.DESTROYED in VALID_LAB_TRANSITIONS[LabStatus.FAULTED]

    def test_destroyed_and_error_terminal(self):
        assert LabStatus.DESTROYED not in VALID_LAB_TRANSITIONS
        assert LabStatus.ERROR not in VALID_LAB_TRANSITIONS


class TestHypothesisTransitions:
    def test_happy_path(self):
        path = [
            HypothesisStatus.DRAFT,
            HypothesisStatus.PROPOSED,
            HypothesisStatus.CRITIC_PASSED,
            HypothesisStatus.APPLIED,
            HypothesisStatus.ACCEPTED,
        ]
        for i in range(len(path) - 1):
            assert path[i + 1] in VALID_HYPOTHESIS_TRANSITIONS[path[i]]

    def test_applied_to_rejected(self):
        assert (
            HypothesisStatus.REJECTED
            in VALID_HYPOTHESIS_TRANSITIONS[HypothesisStatus.APPLIED]
        )

    def test_any_active_state_to_withdrawn(self):
        # "任意→withdrawn（人工）" applies to any non-terminal (active) state;
        # terminal outcomes cannot be withdrawn.
        for state in (
            HypothesisStatus.DRAFT,
            HypothesisStatus.PROPOSED,
            HypothesisStatus.CRITIC_PASSED,
            HypothesisStatus.APPLIED,
        ):
            assert (
                HypothesisStatus.WITHDRAWN
                in VALID_HYPOTHESIS_TRANSITIONS.get(state, set())
            ), state

    def test_terminal_states(self):
        for terminal in (
            HypothesisStatus.ACCEPTED,
            HypothesisStatus.REJECTED,
            HypothesisStatus.WITHDRAWN,
        ):
            assert terminal not in VALID_HYPOTHESIS_TRANSITIONS


class TestIterationTransitions:
    def test_main_loop(self):
        path = [
            IterationState.ATTRIBUTING,
            IterationState.HYPOTHESIZING,
            IterationState.REVIEWING,
            IterationState.APPLYING,
            IterationState.VERIFYING,
            IterationState.DECIDING,
        ]
        for i in range(len(path) - 1):
            assert path[i + 1] in VALID_ITERATION_TRANSITIONS[path[i]]

    def test_reviewing_to_deciding_review_reject_short_circuit(self):
        # Review-gate rejection records a REVERT decision without verification
        # (DD-12 §5.4 short-circuit at the review gate).
        assert (
            IterationState.DECIDING in VALID_ITERATION_TRANSITIONS[IterationState.REVIEWING]
        )

    def test_deciding_to_attributing_next_round(self):
        assert (
            IterationState.ATTRIBUTING
            in VALID_ITERATION_TRANSITIONS[IterationState.DECIDING]
        )

    def test_deciding_to_finished(self):
        assert (
            IterationState.FINISHED in VALID_ITERATION_TRANSITIONS[IterationState.DECIDING]
        )

    def test_any_running_state_to_paused(self):
        running = {
            IterationState.ATTRIBUTING,
            IterationState.HYPOTHESIZING,
            IterationState.REVIEWING,
            IterationState.APPLYING,
            IterationState.VERIFYING,
            IterationState.DECIDING,
        }
        for state in running:
            assert IterationState.PAUSED in VALID_ITERATION_TRANSITIONS[state], state

    def test_any_state_to_aborted(self):
        abortable = set(IterationState) - {
            IterationState.ABORTED,
            IterationState.ARCHIVED,
            IterationState.FINISHED,
        }
        for state in abortable:
            assert IterationState.ABORTED in VALID_ITERATION_TRANSITIONS.get(
                state, set()
            ), state

    def test_finished_or_aborted_to_archived(self):
        assert (
            IterationState.ARCHIVED in VALID_ITERATION_TRANSITIONS[IterationState.FINISHED]
        )
        assert (
            IterationState.ARCHIVED in VALID_ITERATION_TRANSITIONS[IterationState.ABORTED]
        )

    def test_archived_is_terminal(self):
        assert IterationState.ARCHIVED not in VALID_ITERATION_TRANSITIONS


class TestInstanceTransitions:
    def test_creating_to_running(self):
        assert InstanceStatus.RUNNING in VALID_INSTANCE_TRANSITIONS[InstanceStatus.CREATING]

    def test_creating_to_failed(self):
        assert InstanceStatus.FAILED in VALID_INSTANCE_TRANSITIONS[InstanceStatus.CREATING]

    def test_running_to_stopped(self):
        assert InstanceStatus.STOPPED in VALID_INSTANCE_TRANSITIONS[InstanceStatus.RUNNING]

    def test_stopped_to_running(self):
        assert InstanceStatus.RUNNING in VALID_INSTANCE_TRANSITIONS[InstanceStatus.STOPPED]

    def test_stopped_or_running_to_destroyed(self):
        assert (
            InstanceStatus.DESTROYED in VALID_INSTANCE_TRANSITIONS[InstanceStatus.RUNNING]
        )
        assert (
            InstanceStatus.DESTROYED in VALID_INSTANCE_TRANSITIONS[InstanceStatus.STOPPED]
        )

    def test_running_to_failed(self):
        assert InstanceStatus.FAILED in VALID_INSTANCE_TRANSITIONS[InstanceStatus.RUNNING]

    def test_destroyed_and_failed_terminal(self):
        assert InstanceStatus.DESTROYED not in VALID_INSTANCE_TRANSITIONS
        assert InstanceStatus.FAILED not in VALID_INSTANCE_TRANSITIONS


class TestIsValidTransition:
    def test_valid(self):
        assert is_valid_transition(BatchStatus, BatchStatus.CREATED, BatchStatus.RUNNING)

    def test_invalid(self):
        assert not is_valid_transition(
            BatchStatus, BatchStatus.CREATED, BatchStatus.FINALIZED
        )

    def test_accepts_string_values(self):
        assert is_valid_transition(BatchStatus, "created", "running")
        assert not is_valid_transition(BatchStatus, "created", "judging")

    def test_run_status(self):
        assert is_valid_transition(RunStatus, "sut_running", "sut_timeout")
        assert not is_valid_transition(RunStatus, "done", "pending")
