"""Carrier adapters: SUT carrier abstraction + offline fake (P0)."""

from rsi.carrier.adapters.fake import CarrierAdapter, FakeCarrier, RunHandle, TraceChannels
from rsi.carrier.adapters.opencode import OpenCodeAdapter

__all__ = ["CarrierAdapter", "FakeCarrier", "OpenCodeAdapter", "RunHandle", "TraceChannels"]
