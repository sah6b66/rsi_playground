"""Foundation smoke test: DB → GitStore → Security → Models round-trip.

Real components only: SQLite in tmp_path, real bare git repo + worktrees,
argon2 hashing, HS256 JWTs. No mocks.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlmodel import select

from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    MetricDef,
    PrepareAction,
    RootCauseExpectation,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.core.naming import new_id
from rsi.db import create_engine, get_session, init_db
from rsi.db.tables import ALL_TABLES, User
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.versioning.changeset import Changeset, FileChange
from rsi.versioning.gitstore import GitStore

SUT_ID = "test-agent"
TEST_SECRET = "integration-test-secret-0123456789abcdef"  # >=32 bytes for HS256


@pytest.fixture
def store(tmp_path: Path) -> GitStore:
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
    )
    store.init()
    return store


def _seed_sut(store: GitStore) -> None:
    """Create SUT config files in staging and take a baseline snapshot."""
    store.write_file(SUT_ID, "system-prompt.md", "# Initial prompt")
    store.write_file(SUT_ID, "configs/R1.cfg", "hostname R1\n")
    store.snapshot(SUT_ID)
    # tag the initial baseline so promote can fast-forward from v1
    store._commit_staging("seed: initial SUT state", paths=[f"suts/{SUT_ID}"])
    store._tag_baseline(SUT_ID, "v1")


def test_full_foundation(tmp_path: Path, store: GitStore) -> None:
    # ── 1. DB: engine → init_db → 22 tables ───────────────────────────
    engine = create_engine(f"sqlite:///{tmp_path / 'rsi.db'}")
    admin_hash = AuthService(TEST_SECRET).hash_password("admin-pw")
    init_db(engine, admin_user="admin", admin_password_hash=admin_hash)
    tables = sa_inspect(engine).get_table_names()
    assert len(tables) == 22
    assert len(ALL_TABLES) == 22
    with get_session(engine) as session:
        admin = session.exec(
            select(User).where(User.username == "admin")
        ).one()
        assert admin.role == "admin"

    # ── 2. GitStore: SUT files → snapshot → candidate → promote ───────
    _seed_sut(store)
    base = store.snapshot(SUT_ID)
    assert base  # non-empty VersionId

    cs = Changeset(
        hypothesis_id="hyp-001",
        base_version=base,
        files=[
            FileChange(
                path=f"suts/{SUT_ID}/system-prompt.md",
                op="edit",
                content="# Improved prompt",
                target="system_prompt",
                declared=True,
            ),
        ],
    )
    cand = store.commit_candidate(base, cs, "hyp-001")
    assert cand != base
    store.promote(cand)

    # staging synced to promoted content; lineage contains both baselines
    staging = store.worktrees.staging()
    assert "# Improved prompt" in (
        staging / "suts" / SUT_ID / "system-prompt.md"
    ).read_text(encoding="utf-8")
    graph = store.lineage(SUT_ID)
    labels = {n.label for n in graph.nodes}
    assert "suts/test-agent/baseline/v1" in labels
    assert "suts/test-agent/baseline/v2" in labels
    assert any(n.kind == "candidate" for n in graph.nodes)
    # v2's version_id is the candidate commit
    v2 = next(n for n in graph.nodes if n.label.endswith("baseline/v2"))
    assert v2.version_id == str(cand)

    # ── 3. Evalset freeze ─────────────────────────────────────────────
    es_dir = staging / "evalsets" / "es-smoke"
    es_dir.mkdir(parents=True)
    (es_dir / "tasks.jsonl").write_text(
        '{"task_id": "fault/link-down-01"}\n', encoding="utf-8"
    )
    freeze = store.freeze_evalset("es-smoke")
    h = hashlib.sha256()
    for f in sorted(es_dir.rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    assert freeze.content_hash == h.hexdigest()
    assert freeze.git_ref
    # idempotent per content: second freeze hashes identically
    freeze2 = store.freeze_evalset("es-smoke")
    assert freeze2.content_hash == freeze.content_hash

    # ── 4. Security: hash → login → JWT round-trip ────────────────────
    auth = AuthService(TEST_SECRET)
    stored = auth.hash_password("s3cret-pw")
    assert stored.startswith("$argon2")
    assert auth.verify_password("s3cret-pw", stored) is True
    assert auth.verify_password("wrong", stored) is False

    token, claims = auth.login("user-1", "engineer", stored, "s3cret-pw")
    assert token
    decoded = auth.verify(token)
    assert decoded.sub == "user-1"
    assert decoded.role == "engineer"
    assert decoded.jti == claims.jti
    with pytest.raises(Exception):
        auth.verify(token + "x")

    # ── 5. Audit: record an action, entry exists ──────────────────────
    audit = AuditLog()
    audit.record(
        actor="user-1",
        action="promote",
        object_type="sut_version",
        object_id=str(cand),
        detail={"sut_id": SUT_ID},
    )
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.actor == "user-1"
    assert entry.action == "promote"
    assert entry.object_id == str(cand)

    # ── 6. Models round-trip: EnvSpec + TaskRecord ────────────────────
    env = EnvSpec(
        name="smoke-env",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R1:GE0/0/2"))]),
        ssh_access={"R1": SshAccess(ip="192.0.2.1", username="admin")},
    )
    assert env.devices[0].name == "R1"

    task = TaskRecord(
        task_id="fault/bgp-down-01",
        title="BGP session down",
        prepare=[
            PrepareAction(
                action="exec_cli",
                entries=[{"device": "R1", "cli_lines": ["undo bgp 65001"]}],
            )
        ],
        task=TaskSpec(prompt="Diagnose the outage. Output FINAL_JSON."),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="BGP 65001 undone")
        ),
        metrics=[MetricDef(name="root_cause_accuracy")],
    )
    assert task.task_id.startswith("fault/")

    # round-trip via JSON
    assert EnvSpec.model_validate_json(env.model_dump_json()) == env
    assert TaskRecord.model_validate_json(task.model_dump_json()) == task

    # validation rejects bad data
    with pytest.raises(ValueError):
        EnvSpec(
            name="bad",
            backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="c")],
            topology=Topology(),
            ssh_access={},  # missing key -> ssh_access must equal device names
        )
    with pytest.raises(ValueError):
        TaskRecord(
            task_id="invalid-id",
            title="t",
            task=TaskSpec(prompt="p"),
            expected=ExpectedAnswer(),
        )

    # naming still works alongside
    assert new_id("run").startswith("run-")
