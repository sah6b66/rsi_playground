"""Tests for security fixes C6-C8: pre-receive hook, per-request disabled
check, CredentialStore/AuditLog SQLite persistence."""

from __future__ import annotations

import stat

import pytest
from sqlmodel import SQLModel, Session, select

from rsi.core.errors import AuthError
from rsi.db.tables import AuditLogEntry, Credential
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.security.credentials import CredentialStore
from rsi.versioning.hooks import PRE_RECEIVE_SCRIPT, install_hooks


# ── C6: pre-receive hook ────────────────────────────────────────────


class TestPreReceiveHook:
    def test_hook_installed_and_executable(self, tmp_path):
        hook = install_hooks(tmp_path / "repo.git")
        assert hook.exists()
        # Executable bit set (POSIX only; Windows filesystems ignore it).
        import sys

        if sys.platform != "win32":
            assert hook.stat().st_mode & stat.S_IEXEC

    def test_hook_script_content(self, tmp_path):
        hook = install_hooks(tmp_path / "repo.git")
        content = hook.read_text(encoding="utf-8")
        assert content == PRE_RECEIVE_SCRIPT
        assert content.startswith("#!/bin/bash")
        # Rule 1: optimizer identity cannot modify judge/ or prompts/
        assert "optimizer identity cannot modify judge/ or prompts/" in content
        # Rule 2: protected baseline / freeze tags
        assert "refs/tags/suts/*/baseline/*" in content
        assert "refs/tags/evalsets/*/freeze/*" in content
        assert "Cannot modify protected tag" in content
        # Rule 3: frozen evalsets protected from optimizer identity
        assert "evalsets/" in content

    def test_gitstore_init_installs_hook(self, tmp_path):
        import git

        from rsi.versioning.gitstore import GitStore

        store = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wt"
        )
        store.init()
        hook = store.repo_path / "hooks" / "pre-receive"
        assert hook.exists()
        assert hook.read_text(encoding="utf-8") == PRE_RECEIVE_SCRIPT


# ── C7: per-request disabled check ──────────────────────────────────


class TestGetCurrentUser:
    def _auth(self) -> AuthService:
        return AuthService(secret="test-secret")

    def _store(self, disabled: bool = False) -> dict[str, dict]:
        return {
            "u-1": {
                "disabled": disabled,
                "username": "alice",
                "role": "researcher",
            }
        }

    def test_valid_token_enabled_user(self):
        auth = self._auth()
        token, _ = auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        info = auth.get_current_user(token, self._store())
        assert info == {"id": "u-1", "username": "alice", "role": "researcher"}

    def test_valid_token_disabled_user(self):
        auth = self._auth()
        token, _ = auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        with pytest.raises(AuthError):
            auth.get_current_user(token, self._store(disabled=True))

    def test_invalid_token(self):
        auth = self._auth()
        with pytest.raises(AuthError):
            auth.get_current_user("garbage", self._store())

    def test_user_not_found(self):
        auth = self._auth()
        token, _ = auth.login("u-ghost", "researcher", auth.hash_password("pw"), "pw")
        with pytest.raises(AuthError):
            auth.get_current_user(token, self._store())


# ── C8: CredentialStore + AuditLog persistence ──────────────────────

MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # base64(32 bytes)


@pytest.fixture
def session(tmp_path):
    engine = __import__("rsi.db", fromlist=["create_engine"]).create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestCredentialStorePersistence:
    def test_put_resolve_roundtrip_via_db(self, session):
        # Fresh store over the same DB (empty cache) must resolve from DB.
        store = CredentialStore(MASTER_KEY, session=session)
        store.put("dev-ssh", "device_ssh", "s3cret")

        fresh = CredentialStore(MASTER_KEY, session=session)
        assert "dev-ssh" not in fresh._by_name  # not cached yet
        assert fresh.resolve("dev-ssh") == "s3cret"

        row = session.exec(
            select(Credential).where(Credential.name == "dev-ssh")
        ).one()
        assert row.kind == "device_ssh"
        assert b"s3cret" not in row.cipher_blob  # stored encrypted

    def test_memory_only_mode_unchanged(self):
        store = CredentialStore(MASTER_KEY)
        cred_id = store.put("nms", "nms_api", "tok")
        assert store.resolve("nms") == "tok"
        assert store.resolve_id("nms") == cred_id
        with pytest.raises(Exception):
            store.resolve("missing")


class TestAuditLogPersistence:
    def test_record_persisted_to_db(self, session):
        log = AuditLog(session=session)
        log.record(
            actor="u-admin",
            action="sut.create",
            object_type="sut",
            object_id="sut-1",
            detail={"name": "sut-1"},
        )
        rows = session.exec(select(AuditLogEntry)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == "u-admin"
        assert row.action == "sut.create"
        assert row.object_id == "sut-1"
        assert '"name": "sut-1"' in (row.detail_json or "")

    def test_memory_only_mode_unchanged(self):
        log = AuditLog()
        log.record(actor="a", action="x", object_type="t", object_id="1")
        assert len(log.entries) == 1
        assert log.entries[0].actor == "a"
