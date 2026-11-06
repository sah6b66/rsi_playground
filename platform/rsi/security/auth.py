"""JWT authentication service (DD-03 §4.1).

Hashing: argon2-cffi ``PasswordHasher``.
Tokens: PyJWT HS256, 12h expiry, uuid4 jti.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import BaseModel

from rsi.core.errors import AuthError

_ALGORITHM = "HS256"
_GENERIC_LOGIN_ERROR = "invalid credentials"


class Claims(BaseModel):
    """JWT payload fields."""

    sub: str  # user id
    role: str
    exp: int
    iat: int
    jti: str


class AuthService:
    """Password hashing plus JWT issue/verify.

    Optional ``session_factory`` (returns a context-managed SQLModel Session)
    enables per-request disabled checks against the live ``users`` table
    (DD-03 §4.1, ADR-19); without it the in-memory ``users_store`` dict is
    the only source. Optional ``audit_log`` records successful logins as
    ``user.login`` audit entries.
    """

    def __init__(
        self,
        secret: str,
        expire_hours: float = 12.0,
        session_factory=None,
        audit_log=None,
    ) -> None:
        self._secret = secret
        self._expire_seconds = int(timedelta(hours=expire_hours).total_seconds())
        self._hasher = PasswordHasher()
        self._session_factory = session_factory
        self._audit_log = audit_log

    # -- passwords ---------------------------------------------------------

    def hash_password(self, raw: str) -> str:
        """Hash a raw password with argon2."""
        return self._hasher.hash(raw)

    def verify_password(self, raw: str, hashed: str) -> bool:
        """Constant-answer password check; never raises on mismatch."""
        try:
            return self._hasher.verify(hashed, raw)
        except Exception:  # mismatch, invalid hash, etc. -> just False
            return False

    # -- tokens ------------------------------------------------------------

    def login(
        self, user_id: str, role: str, stored_hash: str, password: str
    ) -> tuple[str, Claims]:
        """Verify password against stored argon2 hash, then issue a JWT.

        Raises AuthError on any failure, with a generic message that does not
        reveal whether the user exists or the password was wrong.
        """
        try:
            self._hasher.verify(stored_hash, password)
        except Exception:
            raise AuthError(_GENERIC_LOGIN_ERROR) from None
        now = int(time.time())
        claims = Claims(
            sub=user_id,
            role=role,
            exp=now + self._expire_seconds,
            iat=now,
            jti=str(uuid.uuid4()),
        )
        token = jwt.encode(claims.model_dump(), self._secret, algorithm=_ALGORITHM)
        if self._audit_log is not None:
            try:
                self._audit_log.record(
                    actor=user_id,
                    action="user.login",
                    object_type="user",
                    object_id=user_id,
                    detail={"role": role},
                )
            except Exception:  # noqa: BLE001 - audit must not break login
                pass
        return token, claims

    def verify(self, token: str) -> Claims:
        """Decode and validate a JWT; raise AuthError if invalid or expired."""
        try:
            payload: dict[str, Any] = jwt.decode(
                token, self._secret, algorithms=[_ALGORITHM]
            )
        except Exception:
            raise AuthError("invalid or expired token") from None
        try:
            return Claims.model_validate(payload)
        except Exception:
            raise AuthError("invalid token claims") from None

    def get_current_user(
        self, token: str, users_store: dict[str, dict]
    ) -> dict:
        """Verify JWT -> check user disabled -> return user info.

        ``users_store`` maps user_id -> {"disabled": bool, "username": str,
        "role": str}. Raises AuthError if the token is invalid/expired, the
        user is not found, or the account is disabled (DD-03 §4, ADR-19).
        """
        claims = self.verify(token)
        # DB-first disabled check: a users.disabled change in the DB must
        # take effect immediately, even when the in-memory dict is stale.
        db_username: str | None = None
        if self._session_factory is not None:
            try:
                db_username, disabled = self._db_disabled(claims.sub)
            except Exception:  # noqa: BLE001 - DB hiccup -> memory fallback
                db_username, disabled = None, None
            if disabled is True:
                raise AuthError("Account disabled")
            if db_username is not None:
                return {
                    "id": claims.sub,
                    "username": db_username,
                    "role": claims.role,
                }
        user = users_store.get(claims.sub)
        if user is None:
            raise AuthError("User not found")
        if user.get("disabled", False):
            raise AuthError("Account disabled")
        return {"id": claims.sub, "username": user["username"], "role": claims.role}

    def _db_disabled(self, user_key: str) -> tuple[str | None, bool | None]:
        """Query users by username then id; return (username, disabled).

        Returns ``(None, None)`` when the user is not in the DB at all so the
        caller can fall back to the in-memory store (mixed-mode rigs).
        """
        from sqlmodel import select

        from rsi.db.tables import User

        with self._session_factory() as session:  # type: ignore[misc]
            row = session.exec(
                select(User).where(User.username == user_key)
            ).first()
            if row is None:
                row = session.get(User, user_key)
            if row is None:
                return None, None
            return row.username, bool(row.disabled)


__all__ = ["AuthService", "Claims"]
