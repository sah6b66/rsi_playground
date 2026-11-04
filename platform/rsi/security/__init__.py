"""rsi.security: auth, RBAC, credentials, audit and secret hygiene (DD-03)."""

from rsi.security.audit import AuditEntry, AuditLog, SENSITIVE_FIELDS
from rsi.security.auth import AuthService, Claims
from rsi.security.credentials import (
    CredentialResolver,
    CredentialStore,
    ResolvedCreds,
    SecretFingerprint,
)
from rsi.security.hygiene import SecretHygiene
from rsi.security.rbac import ROLE_PERMISSIONS, Permission, Role, require

__all__ = [
    "AuthService",
    "AuditEntry",
    "AuditLog",
    "SENSITIVE_FIELDS",
    "Claims",
    "CredentialStore",
    "CredentialResolver",
    "ResolvedCreds",
    "SecretFingerprint",
    "SecretHygiene",
    "Role",
    "Permission",
    "ROLE_PERMISSIONS",
    "require",
]
