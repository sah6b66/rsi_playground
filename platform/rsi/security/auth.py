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
    """Password hashing plus JWT issue/verify."""

    def __init__(self, secret: str, expire_hours: float = 12.0) -> None:
        self._secret = secret
        self._expire_seconds = int(timedelta(hours=expire_hours).total_seconds())
        self._hasher = PasswordHasher()

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


__all__ = ["AuthService", "Claims"]
