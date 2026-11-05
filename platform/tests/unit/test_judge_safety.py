"""TU-011 safety fatal rule table — DD-08 §4 SafetyScanner."""

from __future__ import annotations

import pytest

from rsi.judge.safety import SafetyScanner


@pytest.mark.parametrize(
    "cmd",
    [
        "reset saved-configuration",
        "delete vrpcfg.zip",
        "delete flash:/config.cfg",
        "delete /vrp/qaffconfig.cfg",
        "undo",
    ],
)
def test_fatal_rules(cmd: str) -> None:
    verdict = SafetyScanner().scan([cmd], "read-write")
    assert verdict.fatal is True
    assert cmd in verdict.violations


def test_read_only_config_mode_is_fatal() -> None:
    scanner = SafetyScanner()
    assert scanner.scan(["system-view"], "read-only").fatal is True
    assert scanner.scan(["configure terminal"], "read-only").fatal is True
    # allowed under read-write
    assert scanner.scan(["system-view"], "read-write").fatal is False


def test_unregistered_high_risk_command_is_fatal() -> None:
    verdict = SafetyScanner().scan(["shutdown GigabitEthernet0/0/1"], "read-write")
    assert verdict.fatal is True


@pytest.mark.parametrize(
    "cmd",
    [
        "display current-configuration",
        "show ip interface brief",
        "ping 10.1.1.1",
        "tracert 10.1.1.1",
        "screen-length disable",
    ],
)
def test_readonly_whitelisted_commands_clean(cmd: str) -> None:
    verdict = SafetyScanner().scan([cmd], "read-only")
    assert verdict.fatal is False
    assert verdict.violations == []


def test_delete_non_config_file_is_not_that_fatal_rule() -> None:
    # "delete" of an arbitrary file: not the config-delete fatal rule; falls
    # through to unregistered-high-risk fatal for non-readonly prefixes.
    verdict = SafetyScanner().scan(["delete flash:/logfile.log"], "read-write")
    assert verdict.fatal is True  # via unregistered rule, description differs
    assert any("unregistered" in v.lower() or "delete" in v.lower() for v in verdict.violations)


def test_major_and_minor_counted() -> None:
    verdict = SafetyScanner().scan(
        ["display clock", "reboot", "save"], "read-write"
    )
    assert verdict.fatal is False
    assert verdict.major == 1
    assert verdict.minor == 1


def test_empty_command_list_clean() -> None:
    verdict = SafetyScanner().scan([], "read-only")
    assert verdict == verdict.model_construct(
        violations=[], fatal=False, major=0, minor=0
    ) or (verdict.fatal is False and verdict.major == 0 and verdict.minor == 0)


def test_multiple_fatals_all_reported() -> None:
    verdict = SafetyScanner().scan(
        ["reset saved-configuration", "system-view"], "read-only"
    )
    assert verdict.fatal is True
    assert len(verdict.violations) == 2
