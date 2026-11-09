"""Sandbox backend adapters (DD-04 §2)."""

from rsi.sandbox.backends.base import BackendStatus, SandboxBackend
from rsi.sandbox.backends.fake import FakeDevice, FakeSandboxBackend
from rsi.sandbox.backends.prebuilt import PrebuiltBackend

__all__ = [
    "BackendStatus",
    "FakeDevice",
    "FakeSandboxBackend",
    "PrebuiltBackend",
    "SandboxBackend",
]
