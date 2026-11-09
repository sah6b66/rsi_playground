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

    def test_audited_records_client_ip(self):
        # A-11②: a request-like object in kwargs contributes detail["ip"]
        import types

        log = AuditLog()

        @log.audited("iteration.pause", object_type="iteration")
        def pause(request=None, actor: str = "system", object_id: str = ""):
            return "ok"

        req = types.SimpleNamespace(client=types.SimpleNamespace(host="203.0.113.9"))
        assert pause(request=req, actor="u1", object_id="it-1") == "ok"
        e = log.entries[-1]
        assert e.detail["ip"] == "203.0.113.9"
        # no request -> no ip key (sync path without a request is skipped)
        pause(actor="u2", object_id="it-2")
        assert "ip" not in log.entries[-1].detail

    def test_detail_json_truncated_to_2kb(self, tmp_path):
        # A-11②: persisted detail_json is capped at 2 KiB + marker
        from sqlmodel import SQLModel

        from rsi.db import create_engine, get_session
        from rsi.db.tables import AuditLogEntry
        from rsi.security.audit import DETAIL_JSON_MAX, TRUNCATION_MARK

        engine = create_engine(f"sqlite:///{tmp_path}/audit.db")
        SQLModel.metadata.create_all(engine)
        log = AuditLog(session_factory=lambda: get_session(engine))
        log.record("u1", "evalset.import", "evalset", "es-1",
                   detail={"blob": "x" * 3072})
        with get_session(engine) as s:
            row = s.query(AuditLogEntry).one()
        assert len(row.detail_json) <= DETAIL_JSON_MAX + len(TRUNCATION_MARK)
        assert row.detail_json.endswith(TRUNCATION_MARK)
        # short detail passes through untouched
        log.record("u1", "evalset.import", "evalset", "es-2", detail={"k": "v"})
        with get_session(engine) as s:
            rows = s.query(AuditLogEntry).order_by(AuditLogEntry.id).all()
        assert rows[1].detail_json == '{"k": "v"}'


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

    def test_failure_uses_logger_critical(self, monkeypatch):
        # A-11③: startup failure goes through structlog, not print
        from rsi.security import hygiene

        calls = []
        monkeypatch.setattr(
            hygiene._logger, "critical",
            lambda event, **kw: calls.append((event, kw)),
        )
        with pytest.raises(SystemExit):
            SecretHygiene.startup_check(None, GOOD_JWT)
        assert calls and calls[0][0] == "secret_hygiene_check_failed"
        assert "RSI_MASTER_KEY" in calls[0][1]["problems"]

