"""Database engine, session management, and initialization (Task 5).

SQLite with WAL journaling and a 5s busy timeout. See
docs/_rsi_playground_interfaces.md §2.2 for the authoritative schema.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator, TypeVar

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select
from sqlmodel import create_engine as _create_engine

from rsi.db.tables import ALL_TABLES, User  # noqa: F401 (registers tables)

T = TypeVar("T")

__all__ = [
    "ALL_TABLES",
    "create_engine",
    "get_session",
    "init_db",
    "run_db",
]


def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine(url: str) -> Engine:
    """Create a SQLAlchemy engine with SQLite WAL + busy_timeout pragmas."""
    engine = _create_engine(url, echo=False)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def init_db(engine: Engine, admin_user: str, admin_password_hash: str) -> None:
    """Create all tables and seed (or refresh) the admin user."""
    SQLModel.metadata.create_all(engine)
    with get_session(engine) as session:
        existing = (
            session.exec(select(User).where(User.username == admin_user)).first()
        )
        if existing is None:
            session.add(
                User(
                    id=str(uuid.uuid4()),
                    username=admin_user,
                    role="admin",
                    password_hash=admin_password_hash,
                    disabled=0,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        else:
            # A-11①: a re-run of init_db must NOT overwrite the existing
            # admin's role/password (e.g. after a rotation). Seeding is
            # first-run only.
            pass
        session.commit()


@contextmanager
def get_session(engine: Engine) -> Iterator[Session]:
    """Yield a Session; commit on success, rollback on error."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(fn: Callable[[Session], T], engine: Engine) -> T:
    """Run a synchronous session function in a worker thread."""
    def _run() -> T:
        with get_session(engine) as session:
            return fn(session)

    return await asyncio.to_thread(_run)
