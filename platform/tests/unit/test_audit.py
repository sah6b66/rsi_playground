import base64

import pytest

from rsi.security.audit import SENSITIVE_FIELDS, AuditLog, sanitize
from rsi.security.credentials import SecretFingerprint
from rsi.security.hygiene import SecretHygiene

MASTER_KEY = base64.b64encode(b"test-key-32-bytes-1234567890!!!!").decode()
GOOD_JWT = "x" * 32


class TestAuditLog:
    def test_record_fields(self):
        log = AuditLog()
        log.record("u1", "sut.create", "sut", "sut-1", detail={"k": "v"})
        e = log.entries[0]
        assert e.actor == "u1" and e.action == "sut.create"
        assert e.object_type == "sut" and e.object_id == "sut-1"
        assert e.detail == {"k": "v"}
        assert "T" in e.ts  # UTC ISO

    def test_audited_decorator(self):
        log = AuditLog()

        @log.audited("user.create", object_type="user")
        def create_user(username: str, actor: str = "system", object_id: str = ""):
            return username

        assert create_user("bob", actor="admin-1", object_id="u-9") == "bob"
        e = log.entries[-1]
        assert e.actor == "admin-1"
        assert e.action == "user.create"
        assert e.object_id == "u-9"
        assert e.detail["username"] == "bob"

    def test_audited_masks_sensitive(self):
        log = AuditLog()

        @log.audited("credential.put", object_type="credential")
        def put(name: str, secret: str = "", actor: str = "a") -> str:
            return name

        put("nms", secret="hunter2")
        e = log.entries[-1]
        assert e.detail["secret"] == "***"
        assert "hunter2" not in str(e.detail)

    def test_sanitize_recursive(self):
        out = sanitize({"a": 1, "token": "t", "nested": {"api_key": "k", "b": [2]}})
        assert out == {"a": 1, "token": "***", "nested": {"api_key": "***", "b": [2]}}

    def test_sensitive_fields(self):
        assert SENSITIVE_FIELDS == {"password", "secret", "token", "api_key"}


class TestSecretHygiene:
    def test_pass(self):
        SecretHygiene.startup_check(MASTER_KEY, GOOD_JWT)  # no raise

    def test_missing_master_key_exits(self):
        with pytest.raises(SystemExit):
            SecretHygiene.startup_check(None, GOOD_JWT)

    def test_missing_jwt_exits(self):
        with pytest.raises(SystemExit):
            SecretHygiene.startup_check(MASTER_KEY, None)

    def test_short_jwt_exits(self):
        with pytest.raises(SystemExit):
            SecretHygiene.startup_check(MASTER_KEY, "short")

    def test_short_master_key_exits(self):
        short = base64.b64encode(b"8bytes").decode()
        with pytest.raises(SystemExit):
            SecretHygiene.startup_check(short, GOOD_JWT)

    def test_log_filter_masks(self):
        fp = SecretFingerprint(sha8="abcd1234", patterns=["pw-secret", base64.b64encode(b"pw-secret").decode()])
        proc = SecretHygiene.install_log_filter([fp])
        out = proc(None, "info", {"event": "connected with pw-secret", "other": 1})
        assert out["event"] == "connected with ***"
        assert out["other"] == 1
