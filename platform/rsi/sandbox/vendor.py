"""Vendor profile SSOT (DD-04 §3).

Each profile pins the netmiko device type, per-line anchored error patterns
(DD-04 §5.2) and config-view / reset / save command templates so the rest of
the sandbox layer never hard-codes vendor CLI.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 逐行锚定错误模式（DD-04 §5.2；vrp/ce 同一套，ios 补充 cisco 风格）
_ERROR_PATTERNS_COMMON: tuple[str, ...] = (
    r"^Error:",
    r"^Unrecognized command",
    r"^Invalid input",
    r"^%.*\^",
    r"Wrong parameter",
)


class VendorProfile(BaseModel):
    """Single vendor CLI behavior profile."""

    netmiko_device_type: str
    error_patterns: tuple[str, ...] = Field(default_factory=tuple)
    reset_cmd: tuple[str, ...] = Field(default_factory=tuple)
    enter_config: str
    exit_config: str
    save_config: tuple[str, ...] = Field(default_factory=tuple)


_HUAWEI = VendorProfile(
    netmiko_device_type="huawei",
    error_patterns=_ERROR_PATTERNS_COMMON,
    reset_cmd=("reset saved-configuration",),
    enter_config="system-view",
    exit_config="return",
    save_config=("save", "y"),
)

_CISCO = VendorProfile(
    netmiko_device_type="cisco_ios",
    error_patterns=_ERROR_PATTERNS_COMMON,
    reset_cmd=("write erase",),
    enter_config="configure terminal",
    exit_config="end",
    save_config=("write memory",),
)

VENDOR_MAP: dict[str, VendorProfile] = {
    "vrp": _HUAWEI,
    "ce": _HUAWEI,  # ce 复用 vrp 配置（P0 收敛，DD-04 §3）
    "ios": _CISCO,
}
