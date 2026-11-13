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
    # 配置视图提示符正则：VRP5/VRP8 形如 [~?]name[/sub]（~ = 未提交标记，
    # commit 后消失；接口子视图含 /）。user 视图 <name>。
    config_prompt_pattern: str = r"\[~?[A-Za-z0-9._/*-]+\]"
    user_prompt_pattern: str = r"<[A-Za-z0-9._/-]+>"
    # CE/VRP8 两阶段提交：exit_config 前必须显式 commit，否则 return 触发
    # Y/N/C 交互挂死；S 系 vrp 单阶段（立即生效）不开。
    two_phase_commit: bool = False


_HUAWEI = VendorProfile(
    netmiko_device_type="huawei",
    error_patterns=_ERROR_PATTERNS_COMMON,
    reset_cmd=("reset saved-configuration",),
    enter_config="system-view",
    exit_config="return",
    save_config=("save", "y"),
)

_CE = _HUAWEI.model_copy(update={"two_phase_commit": True})

_CISCO = VendorProfile(
    netmiko_device_type="cisco_ios",
    error_patterns=_ERROR_PATTERNS_COMMON,
    reset_cmd=("write erase",),
    enter_config="configure terminal",
    exit_config="end",
    save_config=("write memory",),
    config_prompt_pattern=r"[A-Za-z0-9._-]+\(config[^)]*\)#",
    user_prompt_pattern=r"[A-Za-z0-9._-]+#",
)

VENDOR_MAP: dict[str, VendorProfile] = {
    "vrp": _HUAWEI,
    "ce": _CE,  # CE/VRP8：同 vrp 命令面 + 两阶段提交（D2，S11 CE6800 实测）
    "ios": _CISCO,
}
