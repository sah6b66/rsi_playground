"""Shared pytest fixtures."""

import os

import pytest


@pytest.fixture(autouse=True)
def _rsi_clean_env(monkeypatch):
    """Remove ambient RSI_* env vars before each test for determinism."""
    for key in list(os.environ):
        if key.startswith("RSI_"):
            monkeypatch.delenv(key, raising=False)
