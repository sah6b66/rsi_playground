"""Compatibility shim: the implementation lives in :mod:`rsi.db` (package)."""

from rsi.db import ALL_TABLES, create_engine, get_session, init_db, run_db

__all__ = ["ALL_TABLES", "create_engine", "get_session", "init_db", "run_db"]
