"""Credential encryption, resolution and fingerprinting (DD-03 §4).

P0 foundation: secrets live in an in-memory store encrypted with Fernet
(the master key comes from ``RSI_MASTER_KEY``, base64 of 32 bytes). No DB
persistence yet; no delete — credentials are only deactivated via user
management so history stays traceable.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from rsi.core.errors import AuthError, RsiError
from rsi.core.models import EnvSpec
from rsi.core.naming import new_id
from rsi.db.tables import Credential

CREDENTIAL_KINDS = ("device_ssh", "nms_api", "llm_api", "backend_ssh")


class SecretFingerprint(BaseModel):
    """Masking hint for the log redaction filter."""

    sha8: str  # sha256(secret)[:8] — safe for audit detail
    patterns: list[str] = Field(default_factory=list)  # [secret, base64(secret)]


class ResolvedCreds(BaseModel):
    """CredentialResolver product, consumed by carrier injection (DD-05)."""

    device_env: dict[str, dict[str, str]]
    nms_token: str | None = None
    fingerprints: list[SecretFingerprint] = Field(default_factory=list)


class CredentialStore:
    """Fernet-encrypted credential store.

    In-memory by default (P0). When an optional SQLModel ``session`` is
    supplied, credentials are also persisted to the ``credentials`` table
    (DD-03 §4); the in-memory dicts then act as a cache.
    """

    def __init__(self, master_key: str, session: Session | None = None) -> None:
        try:
            raw = base64.b64decode(master_key, validate=True)
        except Exception as exc:
            raise RsiError("RSI_MASTER_KEY must be base64", context={}) from exc
        if len(raw) != 32:
            raise RsiError(
                f"RSI_MASTER_KEY must decode to 32 bytes, got {len(raw)}",
                context={"decoded_bytes": len(raw)},
            )
        # Fernet expects the urlsafe-base64 encoding of the 32-byte key.
        self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        self._session = session
        self._by_name: dict[str, tuple[str, str, str]] = {}  # name -> (id, kind, token)
        self._by_id: dict[str, tuple[str, str, str]] = {}  # id -> (name, kind, token)

    def put(self, name: str, kind: str, secret: str) -> str:
        """Encrypt and store; duplicate names are rejected (rotate instead)."""
        if kind not in CREDENTIAL_KINDS:
            raise RsiError(f"unknown credential kind {kind!r}", kind=kind)
        if name in self._by_name:
            raise RsiError(f"credential name already exists: {name!r}", name=name)
        cred_id = new_id("cred")
        token = self._fernet.encrypt(secret.encode()).decode()
        self._by_name[name] = (cred_id, kind, token)
        self._by_id[cred_id] = (name, kind, token)
        if self._session is not None:
            self._session.add(
                Credential(
                    id=cred_id,
                    name=name,
                    kind=kind,
                    cipher_blob=token.encode(),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            self._session.commit()
        return cred_id

    def resolve(self, name: str) -> str:
        """Decrypt and return the secret stored under ``name``."""
        entry = self._by_name.get(name)
        if entry is None and self._session is not None:
            row = self._session.exec(
                select(Credential).where(Credential.name == name)
            ).first()
            if row is not None:
                entry = (row.id, row.kind, row.cipher_blob.decode())
                self._by_name[name] = entry  # type: ignore[assignment]
                self._by_id[row.id] = entry  # type: ignore[assignment]
        if entry is None:
            raise RsiError(f"unknown credential: {name!r}", name=name)
        return self._decrypt(entry[2])

    def rotate(self, cred_id: str, new_secret: str) -> None:
        """Replace the encrypted value of an existing credential."""
        entry = self._by_id.get(cred_id)
        if entry is None:
            raise RsiError(f"unknown credential id: {cred_id!r}", cred_id=cred_id)
        name, kind, _ = entry
        token = self._fernet.encrypt(new_secret.encode()).decode()
        self._by_name[name] = (cred_id, kind, token)
        self._by_id[cred_id] = (name, kind, token)
        if self._session is not None:
            row = self._session.get(Credential, cred_id)
            if row is not None:
                row.cipher_blob = token.encode()
                self._session.add(row)
                self._session.commit()

    def resolve_id(self, name: str) -> str:
        entry = self._by_name.get(name)
        if entry is None:
            raise RsiError(f"unknown credential: {name!r}", name=name)
        return entry[0]

    def fingerprints_of(self, cred_id: str) -> SecretFingerprint:
        entry = self._by_id.get(cred_id)
        if entry is None:
            raise RsiError(f"unknown credential id: {cred_id!r}", cred_id=cred_id)
        return _fingerprint(self._decrypt(entry[2]))

    def _decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - tamper guard
            raise RsiError("credential decryption failed (wrong master key?)") from exc


def _fingerprint(secret: str) -> SecretFingerprint:
    sha8 = hashlib.sha256(secret.encode()).hexdigest()[:8]
    patterns = [secret, base64.b64encode(secret.encode()).decode()]
    return SecretFingerprint(sha8=sha8, patterns=patterns)


def _env_name(device: str) -> str:
    """Uppercase, non-alphanumeric -> '_' (DEV_<NAME>_HOST etc.)."""
    return re.sub(r"[^A-Z0-9]", "_", device.upper())


class CredentialResolver:
    """Build the per-run credential environment from an EnvSpec."""

    def __init__(self, store: CredentialStore | None = None) -> None:
        self._store = store

    def resolve_env(self, env: EnvSpec, permission: str) -> ResolvedCreds:
        device_env: dict[str, dict[str, str]] = {}
        fingerprints: list[SecretFingerprint] = []
        for device, ssh in env.ssh_access.items():
            if ssh.password is not None:
                secret = ssh.password
            elif ssh.password_ref is not None:
                if self._store is None:
                    raise AuthError(
                        "password_ref requires a CredentialStore",
                        device=device, ref=ssh.password_ref,
                    )
                secret = self._store.resolve(ssh.password_ref)
            else:
                raise RsiError(
                    "ssh_access needs password or password_ref",
                    device=device,
                )
            prefix = f"DEV_{_env_name(device)}"
            vars_: dict[str, str] = {
                f"{prefix}_HOST": ssh.ip,
                f"{prefix}_PORT": str(ssh.port),
                f"{prefix}_USER": ssh.username,
                f"{prefix}_PASS": secret,
            }
            if permission == "read-write":
                vars_["DEV_RW"] = "1"
            device_env[device] = vars_
            fingerprints.append(_fingerprint(secret))

        nms_token: str | None = None
        for dep in env.external_deps:
            if dep.kind == "nms" and dep.auth_ref:
                if self._store is None:
                    raise AuthError(
                        "auth_ref requires a CredentialStore", ref=dep.auth_ref
                    )
                nms_token = self._store.resolve(dep.auth_ref)
                fingerprints.append(_fingerprint(nms_token))

        return ResolvedCreds(
            device_env=device_env, nms_token=nms_token, fingerprints=fingerprints
        )


__all__ = [
    "CREDENTIAL_KINDS",
    "CredentialStore",
    "CredentialResolver",
    "ResolvedCreds",
    "SecretFingerprint",
]
