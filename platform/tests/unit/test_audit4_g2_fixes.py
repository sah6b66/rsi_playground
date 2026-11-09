"""audit4 G-2 fixes: A-12 rollback DB write-back, A-13 freeze hash EOL.

A-12: GitStore.rollback must update suts.baseline_version_id (DD-09 §4
"更新 DB+staging") and idempotently register the rollback target commit.
A-13: freeze_evalset content_hash must be line-ending normalized so the
same content freezes to the same hash on LF and CRLF platforms.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel

from rsi.db import create_engine
from rsi.db.tables import ConfigVersion, EvalSet, Sut
from rsi.security.audit import AuditLog
from rsi.versioning.changeset import Changeset, FileChange
from rsi.versioning.gitstore import GitStore


class _SessionFactory:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        return Session(self.engine)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'g2.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    return _SessionFactory(engine)


@pytest.fixture
def db_rig(tmp_path, session_factory):
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
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
        session_factory=session_factory,
        audit_log=AuditLog(),
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
    return store, session_factory


def _cs(base, files, hyp="hyp-001"):
    return Changeset(hypothesis_id=hyp, base_version=base, files=files)


# ── A-12: rollback writes back suts.baseline_version_id ─────────────

class TestRollbackDbWriteBack:
    def test_rollback_updates_baseline_version_id(self, db_rig):
        store, sf = db_rig
        v1 = store.snapshot("test-agent")
        staging = store.worktrees.staging()
        (staging / "suts" / "test-agent" / "system-prompt.md").write_text("# v2")
        v2 = store._commit_staging("v2 edit")
        cand = store.commit_candidate(
            v2,
            _cs(v2, [
                FileChange(
                    path="suts/test-agent/system-prompt.md", op="edit",
                    content="# promoted", target="system_prompt", declared=True,
                ),
            ]),
            hyp_id="hyp-001",
        )
        store.promote(cand)
        with sf() as s:
            assert s.get(Sut, "test-agent").baseline_version_id == str(cand)
        store.rollback("test-agent", v1)
        with sf() as s:
            sut = s.get(Sut, "test-agent")
            assert sut.baseline_version_id == str(v1)

    def test_rollback_registers_config_version_idempotently(self, db_rig):
        store, sf = db_rig
        v1 = store.snapshot("test-agent")  # snapshot already registers v1
        store.rollback("test-agent", v1)
        store.rollback("test-agent", v1)  # second rollback must not raise
        with sf() as s:
            rows = s.query(ConfigVersion).filter(
                ConfigVersion.id == str(v1)
            ).all()
            assert len(rows) == 1  # no PK duplication
            assert rows[0].kind == "baseline"


# ── A-13: freeze hash EOL normalization ─────────────────────────────

class TestFreezeHashNormalization:
    def test_crlf_and_lf_same_content_hash(self, gitstore):
        """Same logical content, LF vs CRLF bytes -> identical content_hash."""
        lf_hash = self._freeze_with_bytes(gitstore, b"cases: [c1]\n")
        crlf_hash = self._freeze_with_bytes(gitstore, b"cases: [c1]\r\n")
        assert lf_hash == crlf_hash

    @staticmethod
    def _freeze_with_bytes(store: GitStore, payload: bytes) -> str:
        es_dir = store.worktrees.staging() / "evalsets" / "es-eol"
        es_dir.mkdir(parents=True, exist_ok=True)
        (es_dir / "cases.yaml").write_bytes(payload)
        store._commit_staging("eol write")
        return store.freeze_evalset("es-eol").content_hash

    def test_crlf_freeze_idempotent(self, gitstore):
        """CRLF staging content: repeated freeze reuses the same tag."""
        es_dir = gitstore.worktrees.staging() / "evalsets" / "es-crlf"
        es_dir.mkdir(parents=True)
        (es_dir / "tasks.jsonl").write_bytes(b'{"t": 1}\r\n{"t": 2}\r\n')
        gitstore._commit_staging("crlf seed")
        r1 = gitstore.freeze_evalset("es-crlf")
        r2 = gitstore.freeze_evalset("es-crlf")
        assert r1.content_hash == r2.content_hash
        assert r1.git_ref == r2.git_ref
        freeze_tags = [
            t.name for t in gitstore.repo.tags
            if t.name.startswith("evalsets/es-crlf/freeze/")
        ]
        assert freeze_tags == ["evalsets/es-crlf/freeze/v1"]

    def test_verify_freeze_crlf_manifest(self, gitstore):
        """verify_freeze uses the same normalization (audit3 E1 keeps passing)."""
        import hashlib
        staging = gitstore.worktrees.staging()
        judge = staging / "judge"
        judge.mkdir(parents=True)
        f = judge / "prompt.txt"
        f.write_bytes(b"frozen\r\nwith crlf")
        digest = hashlib.sha256(b"frozen\nwith crlf").hexdigest()
        (judge / "freeze.manifest").write_text(
            '{"judge/prompt.txt": "%s"}' % digest
        )
        gitstore._commit_staging("crlf manifest")
        assert gitstore.verify_freeze() is True


@pytest.fixture
def gitstore(tmp_path):
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
    )
    store.init()
    staging = store.worktrees.staging()
    sut_dir = staging / "suts" / "test-agent"
    sut_dir.mkdir(parents=True)
    (sut_dir / "system-prompt.md").write_text("# Initial prompt")
    store._commit_staging("Initial baseline")
    store._tag_baseline("test-agent", "v1")
    return store
