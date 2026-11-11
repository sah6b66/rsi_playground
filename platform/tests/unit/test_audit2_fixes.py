"""Audit-2 fixes (T1-a): hook tag-deletion, GitStore DB registration,
AUDIT_ACTIONS, credential DB-cache shape, auth disabled immediacy,
path-domain validation and freeze idempotency (A-1..A-9)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from rsi.core.errors import AuthError, TargetViolationError
from rsi.db import create_engine
from rsi.db.tables import ConfigVersion, EvalSet, Sut, User
from rsi.optimizer.targets import TargetRegistry
from rsi.security.audit import AUDIT_ACTIONS, AuditLog, require_known_action
from rsi.security.auth import AuthService
from rsi.security.credentials import CredentialStore
from rsi.versioning.changeset import Changeset, FileChange
from rsi.versioning.gitstore import GitStore
from rsi.versioning.hooks import PRE_RECEIVE_SCRIPT, install_hooks

MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # base64(32B)
ZERO = "0" * 40


# ── A-1 / A-9b: pre-receive hook execution tests ────────────────────

def _bash_available() -> bool:
    return shutil.which("bash") is not None


@pytest.fixture
def hook_repo(tmp_path):
    """A temp git repo with one commit, a freeze tag, and the hook installed."""
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True
    )
    # Existing freeze tag for evalset es-1 (rule 3 data).
    subprocess.run(
        ["git", "tag", "evalsets/es-1/freeze/v1"], cwd=repo, check=True
    )
    hook = install_hooks(tmp_path / "hooks-target")
    return repo, hook


def _run_hook(repo, hook, lines, bash_exe=None):
    payload = "".join(f"{line}\n" for line in lines)
    exe = bash_exe or shutil.which("bash")
    return subprocess.run(
        [exe, str(hook)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=repo,
    )


@pytest.mark.skipif(not _bash_available(), reason="bash not on PATH")
class TestHookExecution:
    def test_delete_protected_freeze_tag_rejected(self, hook_repo):
        repo, hook = hook_repo
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(repo, hook, [f"{sha} {ZERO} refs/tags/evalsets/es-1/freeze/v1"])
        assert res.returncode == 1
        assert "Cannot delete protected tag" in res.stderr

    def test_delete_protected_baseline_tag_rejected(self, hook_repo):
        repo, hook = hook_repo
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(
            repo, hook,
            [f"{sha} {ZERO} refs/tags/suts/sut-1/baseline/v2"],
        )
        assert res.returncode == 1

    def test_modify_protected_tag_rejected(self, hook_repo):
        repo, hook = hook_repo
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(
            repo, hook, [f"{sha} {sha} refs/tags/evalsets/es-1/freeze/v1"]
        )
        assert res.returncode == 1
        assert "Cannot modify protected tag" in res.stderr

    def test_plain_branch_deletion_allowed(self, hook_repo):
        repo, hook = hook_repo
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(repo, hook, [f"{sha} {ZERO} refs/heads/feature/x"])
        assert res.returncode == 0

    def test_unprotected_tag_deletion_allowed(self, hook_repo):
        repo, hook = hook_repo
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(repo, hook, [f"{sha} {ZERO} refs/tags/misc-tag"])
        assert res.returncode == 0

    def test_optimizer_push_to_frozen_evalset_content_rejected(self, hook_repo):
        repo, hook = hook_repo
        # Commit touching evalsets/es-1/tasks.jsonl authored by "rsi-optimizer".
        es = repo / "evalsets" / "es-1"
        es.mkdir(parents=True)
        (es / "tasks.jsonl").write_text("{}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git", "commit", "-q", "-m", "opt edit",
                "--author", "rsi-optimizer <opt@example.com>",
            ],
            cwd=repo, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        parent = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(repo, hook, [f"{parent} {sha} refs/heads/cand/x"])
        assert res.returncode == 1
        assert "frozen evalsets" in res.stderr

    def test_optimizer_push_to_unfrozen_evalset_allowed(self, hook_repo):
        repo, hook = hook_repo
        es = repo / "evalsets" / "es-2"  # no freeze tag for es-2
        es.mkdir(parents=True)
        (es / "tasks.jsonl").write_text("{}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git", "commit", "-q", "-m", "opt edit",
                "--author", "rsi-optimizer <opt@example.com>",
            ],
            cwd=repo, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        parent = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        res = _run_hook(repo, hook, [f"{parent} {sha} refs/heads/cand/y"])
        assert res.returncode == 0


class TestHookScriptLogicFallback:
    """Logic-level checks that always run (even without bash)."""

    def test_deletion_check_precedes_skip(self):
        # The protected-tag branch must appear before the deletion `continue`.
        lines = PRE_RECEIVE_SCRIPT.splitlines()
        prot = next(
            i for i, ln in enumerate(lines) if "refs/tags/suts/*/baseline/*" in ln
        )
        skip = next(
            i for i, ln in enumerate(lines)
            if ln.strip().startswith("[") and "continue" in ln
        )
        assert prot < skip, "protected-tag check must precede the deletion skip"

    def test_frozen_ids_from_tag_listing(self):
        assert "git tag -l 'evalsets/*/freeze/*'" in PRE_RECEIVE_SCRIPT
        # the old broken rule must be gone
        assert "^evalsets/.*(/freeze/|^freeze)" not in PRE_RECEIVE_SCRIPT


# ── shared rig: git + sqlite ─────────────────────────────────────────

class _SessionFactory:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        return Session(self.engine)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'gs.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    return _SessionFactory(engine)


@pytest.fixture
def db_rig(tmp_path, session_factory):
    """GitStore with DB wiring + seeded sut/evalset rows."""
    from datetime import datetime, timezone

    with session_factory() as s:
        s.add(
            Sut(
                id="test-agent", name="test-agent", carrier="claude_code",
                image_ref="img:1",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        s.add(
            EvalSet(
                id="test-es", name="test-es", purpose="iterate",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        s.commit()
    audit = AuditLog()
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
        session_factory=session_factory,
        audit_log=audit,
    )
    store.init()
    staging = store.worktrees.staging()
    sut_dir = staging / "suts" / "test-agent"
    sut_dir.mkdir(parents=True)
    (sut_dir / "system-prompt.md").write_text("# Initial")
    es_dir = staging / "evalsets" / "test-es"
    es_dir.mkdir(parents=True)
    (es_dir / "tasks.jsonl").write_text("{}\n")
    store._commit_staging("seed")
    store._tag_baseline("test-agent", "v1")
    return store, audit, session_factory


def _cs(base, files, hyp="hyp-001"):
    return Changeset(hypothesis_id=hyp, base_version=base, files=files)


# ── A-2: GitStore DB registration / audit ───────────────────────────

class TestGitStoreDbRegistration:
    def test_snapshot_registers_config_version(self, db_rig):
        store, _, sf = db_rig
        staging = store.worktrees.staging()
        (staging / "suts" / "test-agent" / "system-prompt.md").write_text("# v2")
        vid = store.snapshot("test-agent")
        with sf() as s:
            row = s.get(ConfigVersion, str(vid))
            assert row is not None
            assert row.sut_id == "test-agent"
            assert row.kind == "baseline"  # DD-09: snapshot -> kind=baseline
            assert row.git_ref == str(vid)

    def test_commit_candidate_registers_candidate(self, db_rig):
        store, _, sf = db_rig
        base = store.snapshot("test-agent")
        vid = store.commit_candidate(
            base,
            _cs(base, [
                FileChange(
                    path="suts/test-agent/system-prompt.md", op="edit",
                    content="# cand", target="system_prompt", declared=True,
                ),
            ]),
            hyp_id="hyp-001",
        )
        with sf() as s:
            row = s.get(ConfigVersion, str(vid))
            assert row is not None
            assert row.kind == "candidate"
            assert row.label == "hyp-001"

    def test_promote_updates_sut_and_audits(self, db_rig):
        store, audit, sf = db_rig
        base = store.snapshot("test-agent")
        cand = store.commit_candidate(
            base,
            _cs(base, [
                FileChange(
                    path="suts/test-agent/system-prompt.md", op="edit",
                    content="# promoted", target="system_prompt", declared=True,
                ),
            ]),
            hyp_id="hyp-001",
        )
        store.promote(cand)
        with sf() as s:
            sut = s.get(Sut, "test-agent")
            assert sut.baseline_version_id == str(cand)
        actions = [e.action for e in audit.entries]
        assert "version.promote" in actions

    def test_rollback_audits(self, db_rig):
        store, audit, _ = db_rig
        v1 = store.snapshot("test-agent")
        store.rollback("test-agent", v1)
        assert "version.rollback" in [e.action for e in audit.entries]

    def test_freeze_updates_evalset_and_audits(self, db_rig):
        store, audit, sf = db_rig
        rec = store.freeze_evalset("test-es")
        with sf() as s:
            row = s.get(EvalSet, "test-es")
            assert row.git_ref == rec.git_ref
            assert row.content_hash == rec.content_hash
            assert row.frozen_at == rec.frozen_at
        assert "evalset.freeze" in [e.action for e in audit.entries]

    def test_db_failure_does_not_break_git(self, tmp_path, db_rig):
        store, _, _ = db_rig

        def boom():
            raise RuntimeError("db down")

        store.session_factory = boom
        staging = store.worktrees.staging()
        (staging / "suts" / "test-agent" / "system-prompt.md").write_text("# v3")
        vid = store.snapshot("test-agent")  # must not raise
        assert vid

    def test_no_db_wiring_backward_compatible(self, tmp_path):
        store = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wt"
        )
        store.init()
        staging = store.worktrees.staging()
        d = staging / "suts" / "x"
        d.mkdir(parents=True)
        (d / "system-prompt.md").write_text("p")
        store._commit_staging("seed")
        assert store.snapshot("x")


# ── A-3: AUDIT_ACTIONS ──────────────────────────────────────────────

class TestAuditActions:
    def test_table_is_nonempty_wellformed(self):
        assert len(AUDIT_ACTIONS) >= 30
        import re

        for action in AUDIT_ACTIONS:
            assert re.fullmatch(r"[a-z_]+\.[a-z_\-]+", action), action

    def test_known_actions_accepted(self):
        for action in ("user.login", "version.promote", "evalset.freeze"):
            require_known_action(action)

    def test_unknown_action_raises(self):
        with pytest.raises(ValueError):
            require_known_action("definitely.not.real")

    def test_unknown_action_in_decorator_warns(self, capsys):
        # structlog prints JSON lines to stdout; caplog does not see them.
        log = AuditLog()

        @log.audited("totally.unknown", object_type="x")
        def op(actor="tester", object_id="1"):
            return 42

        assert op() == 42  # not blocked
        out = capsys.readouterr().out
        assert "totally.unknown" in out
        assert "unknown_action" in out

    def test_known_action_in_decorator_no_warning(self, capsys):
        log = AuditLog()

        @log.audited("sut.update", object_type="sut")
        def op(actor="tester", object_id="s-1"):
            return 1

        op()
        out = capsys.readouterr().out
        assert "unknown_action" not in out


# ── A-4: CredentialStore DB cache tuple shape ────────────────────────

class TestCredentialDbShape:
    def test_rotate_after_db_load(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            store = CredentialStore(MASTER_KEY, session=s)
            cred_id = store.put("dev-ssh", "device_ssh", "old-secret")

        # Fresh instance: cache only populated via DB load path.
        with Session(engine) as s:
            fresh = CredentialStore(MASTER_KEY, session=s)
            assert fresh.resolve("dev-ssh") == "old-secret"
            # _by_id entry must be (name, kind, token) — not (id, kind, token)
            assert fresh._by_id[cred_id][0] == "dev-ssh"
            fresh.rotate(cred_id, "new-secret")
            # rotate must not create a bogus _by_name[cred_id] entry
            assert "dev-ssh" in fresh._by_name
            assert cred_id not in fresh._by_name
            assert fresh.resolve("dev-ssh") == "new-secret"
            fp = fresh.fingerprints_of(cred_id)
            assert fp.sha8  # fingerprint works via correctly-shaped entry


# ── A-5: AuthService disabled immediacy + login audit ────────────────

class TestAuthDisabledDb:
    def _db_auth(self, session_factory, audit=None):
        return AuthService(
            secret="s3cret", session_factory=session_factory, audit_log=audit
        )

    def test_db_disabled_wins_over_memory(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            s.add(
                User(
                    id="u-1", username="alice", role="researcher",
                    password_hash="x", disabled=1,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            s.commit()
        auth = self._db_auth(_SessionFactory(engine))
        token, _ = auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        memory_says_enabled = {"u-1": {"disabled": False, "username": "alice"}}
        with pytest.raises(AuthError):
            auth.get_current_user(token, memory_says_enabled)

    def test_db_enabled_serves_from_db(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            s.add(
                User(
                    id="u-1", username="alice", role="researcher",
                    password_hash="x", disabled=0,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            s.commit()
        auth = self._db_auth(_SessionFactory(engine))
        token, _ = auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        info = auth.get_current_user(token, {})  # empty memory store
        assert info["username"] == "alice"

    def test_unknown_user_falls_back_to_memory(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}")
        SQLModel.metadata.create_all(engine)
        auth = self._db_auth(_SessionFactory(engine))
        token, _ = auth.login("u-9", "researcher", auth.hash_password("pw"), "pw")
        store = {"u-9": {"disabled": False, "username": "bob"}}
        assert auth.get_current_user(token, store)["username"] == "bob"

    def test_login_audited(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
        SQLModel.metadata.create_all(engine)
        audit = AuditLog()
        auth = self._db_auth(_SessionFactory(engine), audit=audit)
        auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        logins = [e for e in audit.entries if e.action == "user.login"]
        assert len(logins) == 1
        assert logins[0].actor == "u-1"

    def test_no_factory_keeps_memory_behavior(self):
        auth = AuthService(secret="s")
        token, _ = auth.login("u-1", "researcher", auth.hash_password("pw"), "pw")
        assert auth.get_current_user(
            token, {"u-1": {"disabled": False, "username": "a"}}
        )["username"] == "a"


# ── A-8: path-domain validation ─────────────────────────────────────

class TestPathDomainValidation:
    def _validated_store(self, tmp_path):
        store = GitStore(
            repo_path=tmp_path / "repo.git",
            worktree_root=tmp_path / "wt",
            target_registry=TargetRegistry(),
        )
        store.init()
        staging = store.worktrees.staging()
        d = staging / "suts" / "t"
        d.mkdir(parents=True)
        (d / "system-prompt.md").write_text("p")
        store._commit_staging("seed")
        store._tag_baseline("t", "v1")
        return store

    def test_commit_candidate_frozen_path_rejected(self, tmp_path):
        store = self._validated_store(tmp_path)
        base = store.snapshot("t")
        n_commits_before = len(
            store.repo.git.log("--oneline").splitlines()
        )
        with pytest.raises(TargetViolationError):
            store.commit_candidate(
                base,
                _cs(base, [
                    FileChange(
                        path="judge/rules.yaml", op="edit", content="evil",
                        target="system_prompt", declared=True,
                    ),
                ]),
                hyp_id="hyp-001",
            )
        # no new commit / branch leaked
        assert len(store.repo.git.log("--oneline").splitlines()) == n_commits_before
        assert not [
            h for h in store.repo.heads if h.name.startswith("suts/t/cand")
        ]

    def test_commit_candidate_out_of_domain_rejected(self, tmp_path):
        store = self._validated_store(tmp_path)
        base = store.snapshot("t")
        with pytest.raises(TargetViolationError):
            store.commit_candidate(
                base,
                _cs(base, [
                    FileChange(
                        path="suts/t/random-dir/x.md", op="create", content="x",
                        target="system_prompt", declared=True,
                    ),
                ]),
                hyp_id="hyp-001",
            )

    def test_write_file_commit_frozen_path_rejected(self, tmp_path):
        # judge/ sits outside suts/ — write_file prefixes suts/<id>/, so use
        # an evalsets-style frozen path via the same prefix trick: the frozen
        # regexes are anchored at repo root, so any suts/<id>/... path that
        # matches a frozen pattern is impossible; instead verify whitelist
        # enforcement (out-of-domain file under suts/).
        store = self._validated_store(tmp_path)
        with pytest.raises(TargetViolationError):
            store.write_file("t", "notes/scratch.md", "x", commit=True)

    def test_write_file_whitelisted_path_ok(self, tmp_path):
        store = self._validated_store(tmp_path)
        store.write_file("t", "system-prompt.md", "# new", commit=True)
        assert store.snapshot("t")  # commit succeeded

    def test_no_registry_keeps_old_behavior(self, tmp_path):
        store = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wt"
        )
        store.init()
        staging = store.worktrees.staging()
        d = staging / "suts" / "t"
        d.mkdir(parents=True)
        (d / "system-prompt.md").write_text("p")
        store._commit_staging("seed")
        store._tag_baseline("t", "v1")
        store.write_file("t", "notes/scratch.md", "x", commit=True)  # no raise


# ── A-9a: freeze idempotency ────────────────────────────────────────

class TestFreezeIdempotency:
    def test_same_content_reuses_tag(self, tmp_path):
        store = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wt"
        )
        store.init()
        staging = store.worktrees.staging()
        es = staging / "evalsets" / "es"
        es.mkdir(parents=True)
        (es / "tasks.jsonl").write_text("{}\n")
        store._commit_staging("seed")

        rec1 = store.freeze_evalset("es")
        tags_after_1 = [t.name for t in store.repo.tags if "freeze" in t.name]
        rec2 = store.freeze_evalset("es")
        tags_after_2 = [t.name for t in store.repo.tags if "freeze" in t.name]

        assert rec2.content_hash == rec1.content_hash
        assert rec2.git_ref == rec1.git_ref
        assert rec2.frozen_at == rec1.frozen_at  # original freeze time kept
        assert tags_after_1 == tags_after_2  # tag count unchanged

    def test_changed_content_creates_new_tag(self, tmp_path):
        store = GitStore(
            repo_path=tmp_path / "repo.git", worktree_root=tmp_path / "wt"
        )
        store.init()
        staging = store.worktrees.staging()
        es = staging / "evalsets" / "es"
        es.mkdir(parents=True)
        (es / "tasks.jsonl").write_text("{}\n")
        store._commit_staging("seed")

        rec1 = store.freeze_evalset("es")
        (es / "tasks.jsonl").write_text('{"t":1}\n')
        store._commit_staging("edit")
        rec2 = store.freeze_evalset("es")
        assert "evalsets/es/freeze/v2" in [t.name for t in store.repo.tags]
        assert rec2.content_hash != rec1.content_hash


# ── F10: production wiring fixes (runs/batches, IterationEngine, write_file) ──


class TestF10WriteFileRegistersVersion:
    def test_write_file_commit_registers_config_version(self, db_rig):
        """F10: write_file(commit=True) 后 config_versions 有行（kind 为
        CHECK 允许的 baseline，label 标注 manual write）。"""
        store, _, sf = db_rig
        from rsi.versioning.gitstore import GitStore  # noqa: F401

        store.write_file(
            "test-agent", "system-prompt.md", "# manual v2", commit=True,
            message="manual edit",
        )
        with sf() as s:
            rows = s.exec(
                select(ConfigVersion).where(ConfigVersion.sut_id == "test-agent")
            ).all()
        assert any("manual write" in (r.label or "") for r in rows)
        manual = next(r for r in rows if "manual write" in (r.label or ""))
        assert manual.kind in ("baseline", "candidate", "archive")  # CHECK enum
        assert manual.git_ref  # real commit sha

    def test_write_file_no_commit_registers_nothing(self, db_rig):
        store, _, sf = db_rig
        with sf() as s:
            before = len(s.exec(select(ConfigVersion)).all())
        store.write_file("test-agent", "notes.md", "staging only")
        with sf() as s:
            after = len(s.exec(select(ConfigVersion)).all())
        assert before == after


class TestF10IterationEngineWiring:
    def test_build_services_wires_engine_components(self, tmp_path):
        """F10: bootstrap 构造的 IterationEngine 带上 change_applier/critic/
        decision_recorder/gitstore（APPLYING/DECIDING 生产链依赖）。"""
        from rsi.bootstrap import build_services
        from rsi.settings import DatabaseSettings, Settings

        services = build_services(
            Settings(
                # C1: home derived from RSI_HOME env
                # (kwarg dropped); point env at tmp first
                database=DatabaseSettings(
                    url=f"sqlite:///{(tmp_path / 'wiring.db').as_posix()}"
                ),
            )
        )
        engine = services["iteration_engine"]
        assert engine.change_applier is not None
        assert engine.critic is not None
        assert engine.decision_recorder is not None
        assert engine.gitstore is services["git_store"]
