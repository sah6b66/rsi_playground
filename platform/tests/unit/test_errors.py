"""Tests for rsi.core.errors (DD-01 §3.12)."""

import pytest

from rsi.core.errors import (
    AuthError,
    BudgetExceeded,
    ConfigError,
    CarrierError,
    DeviceError,
    EnvError,
    FreezeViolationError,
    GitStoreError,
    InvalidTransitionError,
    JudgeError,
    LLMOutputInvalid,
    NamingError,
    NotFastForwardError,
    RsiError,
    TargetViolationError,
    TraceError,
    UnknownDeviceError,
)

ALL_SUBCLASSES = [
    ConfigError,
    NamingError,
    EnvError,
    DeviceError,
    CarrierError,
    TraceError,
    JudgeError,
    LLMOutputInvalid,
    GitStoreError,
    FreezeViolationError,
    NotFastForwardError,
    TargetViolationError,
    BudgetExceeded,
    AuthError,
    InvalidTransitionError,
    UnknownDeviceError,
]


class TestHierarchy:
    @pytest.mark.parametrize("exc", ALL_SUBCLASSES)
    def test_is_rsi_error(self, exc):
        assert issubclass(exc, RsiError)

    def test_rsi_error_is_exception(self):
        assert issubclass(RsiError, Exception)

    def test_freeze_and_ff_are_git_store_errors(self):
        assert issubclass(FreezeViolationError, GitStoreError)
        assert issubclass(NotFastForwardError, GitStoreError)


class TestContext:
    def test_context_stored(self):
        err = DeviceError("ssh failed", output="err", device="r1", cli="show ver")
        assert err.context == {"output": "err", "device": "r1", "cli": "show ver"}

    def test_context_defaults_empty(self):
        err = ConfigError("bad config")
        assert err.context == {}

    def test_message_in_str(self):
        err = ConfigError("bad config")
        assert "bad config" in str(err)

    def test_context_kwargs_rejected_positional(self):
        # message positional, context keyword-only
        err = ConfigError("bad", context={"k": "v"})
        assert err.context == {"k": "v"}


class TestInvalidTransition:
    def test_from_to_in_context(self):
        err = InvalidTransitionError(
            "illegal transition", from_state="FINISHED", to_state="RUNNING"
        )
        assert err.context["from_state"] == "FINISHED"
        assert err.context["to_state"] == "RUNNING"


class TestCatchBase:
    def test_catch_via_base(self):
        with pytest.raises(RsiError):
            raise BudgetExceeded("tokens exhausted", limit=1000)
