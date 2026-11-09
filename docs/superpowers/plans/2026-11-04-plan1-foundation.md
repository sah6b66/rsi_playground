# Plan 1: RSI Playground Foundation (S1+S2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational layer (core models, enums, naming, errors, DB schema, settings, logging, security, and Git version management) that all subsequent subsystems depend on.

**Architecture:** Pure data layer + infrastructure services. No business logic, no network/LLM/Docker dependencies. Everything testable offline with pytest. SQLModel for ORM (SQLite WAL mode), GitPython for bare-repo version management, Pydantic v2 for domain models.

**Tech Stack:** Python 3.12 (uv-managed), Pydantic v2, SQLModel/SQLAlchemy, GitPython, structlog, PyJWT, argon2-cffi, cryptography (Fernet), pytest+pytest-asyncio.

**Spec:** `docs/detail_design/RSI-PG-DD-01-core.md` (models/enums/naming/errors/DB), `docs/detail_design/RSI-PG-DD-03-security.md` (auth/rbac/credentials/audit), `docs/detail_design/RSI-PG-DD-09-versioning.md` (GitStore), `docs/_rsi_playground_configuration.md` §2 (settings).

## Global Constraints

- Python 3.12 (uv manages; system has 3.11 → `uv python install 3.12`)
- All packages under `platform/rsi/`; tests under `platform/tests/`
- Pydantic v2 `ConfigDict(frozen=True, extra="forbid")` for domain models
- SQLModel for ORM tables; DDL source of truth = `docs/_rsi_playground_interfaces.md` §2.2
- SQLite WAL mode + `busy_timeout=5000` (ADR-24 P2)
- Git bare repo at `var/git/platform-repo.git`; worktrees at `var/git/wt/`
- Runtime IDs: `<prefix>-<YYYYMMDDHHmmss UTC>-<8位随机>` (naming.py is sole generator)
- All timestamps UTC ISO8601 milliseconds (string)
- Logging via structlog only (no bare print/logging)
- Tests: pytest, no Docker/network/LLM required (Fake implementations only)
- Commit after each green test cycle

---

### Task 1: Project Scaffold + Settings + Log

**Files:**
- Create: `platform/pyproject.toml`
- Create: `platform/rsi/__init__.py`
- Create: `platform/rsi/settings.py`
- Create: `platform/rsi/log.py`
- Create: `platform/tests/__init__.py`
- Create: `platform/tests/conftest.py`
- Test: `platform/tests/unit/test_settings.py`
- Test: `platform/tests/unit/test_log.py`

**Interfaces:**
- Produces: `Settings` class (all config fields), `get_logger(name)` function, `RSI_HOME` env var handling

- [ ] **Step 1: Create pyproject.toml and directory structure**

```toml
# platform/pyproject.toml
[project]
name = "rsi-playground"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "sqlmodel>=0.0.16",
    "sqlalchemy>=2.0",
    "gitpython>=3.1",
    "structlog>=24.1",
    "pyjwt>=2.8",
    "argon2-cffi>=23.1",
    "cryptography>=42.0",
    "httpx>=0.27",
    "scipy>=1.12",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

```bash
cd platform
uv venv --python 3.12
uv sync
```

- [ ] **Step 2: Write settings.py**

Create `platform/rsi/settings.py` following `docs/_rsi_playground_configuration.md` §2. Use Pydantic `BaseSettings` with `env_prefix="RSI_"`. Include all sections: server, database, git, storage, logging, sandbox, carrier, instrumentor, tracing, judge, optimizer, decision, orchestrator, llm, security, scheduler, seed. Each field has type, default, and comment referencing the config doc section.

Key: `RSI_HOME` env var defaults to `./var`. Relative paths resolve against it.

- [ ] **Step 3: Write log.py**

Create `platform/rsi/log.py` using structlog. Provide `get_logger(name)` that returns a structlog BoundLogger with contextvars for run_id/batch_id/experiment_id. JSON output format. `RSI_LOG_FORMAT=console` switches to colored.

- [ ] **Step 4: Write tests**

```python
# platform/tests/unit/test_settings.py
def test_default_settings():
    from rsi.settings import Settings
    s = Settings()
    assert s.server.port == 8000
    assert s.database.url.startswith("sqlite")
    assert s.git.repo.bare is True
    assert s.security.jwt_expire_hours == 12

def test_env_override():
    import os
    os.environ["RSI_SERVER__PORT"] = "9000"
    s = Settings()
    assert s.server.port == 9000
    del os.environ["RSI_SERVER__PORT"]
```

```python
# platform/tests/unit/test_log.py  
def test_logger_json_output(capsys):
    from rsi.log import get_logger
    logger = get_logger("test.module")
    logger.info("hello", run_id="run-123")
    # Assert JSON output contains run_id
```

- [ ] **Step 5: Run tests and commit**

```bash
cd platform && uv run pytest tests/unit/test_settings.py tests/unit/test_log.py -v
git add platform/ && git commit -m "feat: project scaffold + settings + structlog"
```

---

### Task 2: Enums + State Machine Transition Tables

**Files:**
- Create: `platform/rsi/core/__init__.py`
- Create: `platform/rsi/core/enums.py`
- Test: `platform/tests/unit/test_enums.py`

**Interfaces:**
- Produces: `BatchStatus`, `RunStatus`, `LabStatus`, `EventType`, `HypothesisStatus`, `IterationState`, `InstanceStatus`, `ExperimentDecision` (all `StrEnum`), plus `VALID_TRANSITIONS` dict for each state machine

- [ ] **Step 1: Write failing tests for state machines**

```python
# platform/tests/unit/test_enums.py
import pytest
from rsi.core.enums import BatchStatus, RunStatus, VALID_BATCH_TRANSITIONS

class TestBatchTransitions:
    def test_created_to_running(self):
        assert BatchStatus.RUNNING in VALID_BATCH_TRANSITIONS[BatchStatus.CREATED]

    def test_running_to_judging(self):
        assert BatchStatus.JUDGING in VALID_BATCH_TRANSITIONS[BatchStatus.RUNNING]

    def test_judging_to_finalized(self):
        assert BatchStatus.FINALIZED in VALID_BATCH_TRANSITIONS[BatchStatus.JUDGING]

    def test_judging_to_failed(self):
        assert BatchStatus.FAILED in VALID_BATCH_TRANSITIONS[BatchStatus.JUDGING]

    def test_running_to_aborted(self):
        assert BatchStatus.ABORTED in VALID_BATCH_TRANSITIONS[BatchStatus.RUNNING]

    def test_finalized_is_terminal(self):
        assert BatchStatus.FINALIZED not in VALID_BATCH_TRANSITIONS  # no outgoing

class TestRunTransitions:
    def test_full_happy_path(self):
        from rsi.core.enums import RunStatus as RS, VALID_RUN_TRANSITIONS
        path = [RS.PENDING, RS.ENV_BUILDING, RS.ENV_READY, RS.SUT_RUNNING,
                RS.COLLECTING, RS.JUDGING, RS.DONE]
        for i in range(len(path)-1):
            assert path[i+1] in VALID_RUN_TRANSITIONS[path[i]]

    def test_env_building_to_env_failed(self):
        from rsi.core.enums import RunStatus as RS, VALID_RUN_TRANSITIONS
        assert RS.ENV_FAILED in VALID_RUN_TRANSITIONS[RS.ENV_BUILDING]

    def test_sut_running_to_timeout(self):
        from rsi.core.enums import RunStatus as RS, VALID_RUN_TRANSITIONS
        assert RS.SUT_TIMEOUT in VALID_RUN_TRANSITIONS[RS.SUT_RUNNING]
```

- [ ] **Step 2: Write enums.py**

Create `platform/rsi/core/enums.py` with all 8 StrEnum classes and `VALID_*_TRANSITIONS` dicts per DD-01 §3.11. Include `is_valid_transition(enum_class, from_val, to_val) -> bool` helper.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_enums.py -v
git add platform/rsi/core/ platform/tests/unit/test_enums.py
git commit -m "feat: core enums + state machine transition tables"
```

---

### Task 3: Errors + Naming

**Files:**
- Create: `platform/rsi/core/errors.py`
- Create: `platform/rsi/core/naming.py`
- Test: `platform/tests/unit/test_errors.py`
- Test: `platform/tests/unit/test_naming.py`

**Interfaces:**
- Produces: `RsiError` base + 16 subclasses; `new_id(prefix)`, `validate_id(prefix, value)`, `validate_sut_id`, `validate_evalset_id`, `validate_task_id`, `PREFIXES` frozenset

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_naming.py
import pytest
from rsi.core.naming import new_id, validate_id, validate_sut_id, validate_task_id, PREFIXES
from rsi.core.errors import NamingError

class TestNewId:
    def test_format(self):
        eid = new_id("run")
        parts = eid.split("-")
        assert len(parts) == 3
        assert parts[0] == "run"
        assert len(parts[1]) == 14  # YYYYMMDDHHmmss
        assert len(parts[2]) == 8   # random

    def test_unique(self):
        ids = {new_id("run") for _ in range(100)}
        assert len(ids) == 100

    def test_invalid_prefix(self):
        with pytest.raises(NamingError):
            new_id("bogus")

class TestValidateSutId:
    def test_valid(self):
        validate_sut_id("ipops-agent")  # no exception

    def test_too_short(self):
        with pytest.raises(NamingError):
            validate_sut_id("ab")  # min 3 chars

    def test_uppercase_rejected(self):
        with pytest.raises(NamingError):
            validate_sut_id("MyAgent")

class TestValidateTaskId:
    def test_valid(self):
        validate_task_id("fault/bgp-peer-down-01")

    def test_invalid_category(self):
        with pytest.raises(NamingError):
            validate_task_id("bogus/bgp-peer-down-01")
```

- [ ] **Step 2: Write errors.py and naming.py**

Follow DD-01 §3.12 for errors and §4.1 for naming. Key: naming uses `secrets.token_bytes` → Crockford base32 encoding (exclude i,l,o,u) for random segment.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_errors.py tests/unit/test_naming.py -v
git add platform/rsi/core/errors.py platform/rsi/core/naming.py platform/tests/unit/test_error*.py platform/tests/unit/test_naming.py
git commit -m "feat: core errors + naming with timestamp IDs"
```

---

### Task 4: Domain Models (all Pydantic models)

**Files:**
- Create: `platform/rsi/core/models.py`
- Test: `platform/tests/unit/test_models.py`

**Interfaces:**
- Produces: All models from DD-01 §3.1-3.10 — this is the largest file (~400 lines of Pydantic models). Consumed by every subsequent module.

- [ ] **Step 1: Write failing tests for key model validators**

```python
# platform/tests/unit/test_models.py
import pytest
from rsi.core.models import EnvSpec, DeviceSpec, SshAccess, TaskRecord, PrepareAction, ExpectedAnswer, StateAssert
from rsi.core.errors import NamingError

class TestEnvSpec:
    def test_minimal(self):
        env = EnvSpec(
            name="test-env", backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
            topology={"links": []},
            ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password="test")},
        )
        assert env.devices[0].name == "R1"

    def test_ssh_access_must_cover_devices(self):
        with pytest.raises(Exception):
            EnvSpec(
                name="test", backend="fake",
                devices=[DeviceSpec(name="R1", type="vrp", config="c"),
                         DeviceSpec(name="R2", type="vrp", config="c")],
                topology={"links": []},
                ssh_access={"R1": SshAccess(ip="10.0.0.1", username="a", password="p")},
                # R2 missing → should fail
            )

    def test_ssh_password_or_ref_exclusive(self):
        with pytest.raises(Exception):
            SshAccess(ip="10.0.0.1", username="a", password="p", password_ref="cred-1")

class TestPrepareAction:
    def test_exec_cli(self):
        pa = PrepareAction(action="exec_cli",
                          entries=[{"device": "R1", "cli_lines": ["int GE0/0/1", "shutdown"]}])
        assert pa.entries[0].device == "R1"

    def test_call_api(self):
        pa = PrepareAction(action="call_api", api="cfg-api",
                          params={"env_name": "test", "scenario": "s1",
                                  "devices": [{"device": "R1", "cli_lines": ["cmd"]}]})
        assert pa.params.env_name == "test"

class TestStateAssert:
    def test_requires_at_least_one_expectation(self):
        with pytest.raises(Exception):
            StateAssert(device="R1", cli="display clock", expect_contains=[], expect_absent=[])
```

- [ ] **Step 2: Write models.py**

Create all Pydantic models per DD-01 §3.1-3.10. Use `_M = BaseModel` with `ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)`. Include all `model_validator`s for cross-field constraints. This file is large but pure data — no IO, no logic beyond validation.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_models.py -v
git add platform/rsi/core/models.py platform/tests/unit/test_models.py
git commit -m "feat: complete domain models (EnvSpec, TaskRecord, TraceEvent, Verdict, Hypothesis, OptimizationProfile, etc.)"
```

---

### Task 5: Database (22 SQLModel Tables + WAL)

**Files:**
- Create: `platform/rsi/db.py`
- Create: `platform/rsi/db/__init__.py`
- Create: `platform/rsi/db/tables.py`
- Test: `platform/tests/unit/test_db.py`

**Interfaces:**
- Produces: `create_engine(url)`, `get_session()`, `init_db(engine, admin_user, admin_pw_hash)`, `run_db(fn)`, all 22 SQLModel table classes

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_db.py
import pytest
from sqlalchemy import text
from rsi.db import create_engine, init_db, get_session

class TestDatabase:
    def test_init_creates_all_tables(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")
        with get_session(engine) as s:
            result = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = {row[0] for row in result}
            assert "users" in tables
            assert "evalsets" in tables
            assert "iterations" in tables
            assert "batches" in tables
            assert "runs" in tables
            assert "hypotheses" in tables
            assert len(tables) >= 22  # all tables

    def test_wal_mode(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        with get_session(engine) as s:
            mode = s.execute(text("PRAGMA journal_mode")).scalar()
            assert mode == "wal"

    def test_admin_seed(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hashed")
        with get_session(engine) as s:
            from rsi.db.tables import User
            user = s.query(User).filter_by(username="admin").first()
            assert user is not None
            assert user.role == "admin"
```

- [ ] **Step 2: Write db/tables.py + db.py**

`tables.py`: All 22 SQLModel classes matching interfaces doc §2.2 DDL exactly. Use `__table_args__` for composite PKs and unique constraints. Table creation order: SQLModel.metadata.create_all handles topological sort automatically.

`db.py`: `create_engine()` sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` via event listener. `init_db()` calls `create_all` + seeds admin. `run_db()` wraps sync session ops in `asyncio.to_thread`.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_db.py -v
git add platform/rsi/db.py platform/rsi/db/ platform/tests/unit/test_db.py
git commit -m "feat: 22-table SQLModel schema + WAL mode + admin seed"
```

---

### Task 6: Security — Auth + RBAC

**Files:**
- Create: `platform/rsi/security/__init__.py`
- Create: `platform/rsi/security/auth.py`
- Create: `platform/rsi/security/rbac.py`
- Test: `platform/tests/unit/test_auth.py`
- Test: `platform/tests/unit/test_rbac.py`

**Interfaces:**
- Produces: `AuthService` (login/verify/hash_password), `require(*perms)` FastAPI dependency factory, `ROLE_PERMISSIONS` dict

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_auth.py
import pytest
from rsi.security.auth import AuthService

class TestAuthService:
    def test_hash_and_verify(self):
        svc = AuthService(secret="test-secret")
        hashed = svc.hash_password("mypassword")
        assert svc.verify_password("mypassword", hashed)
        assert not svc.verify_password("wrong", hashed)

    def test_login_and_verify_roundtrip(self):
        svc = AuthService(secret="test-secret")
        token, claims = svc.login(user_id="u-1", role="admin",
                                   stored_hash=svc.hash_password("pw"))
        assert claims.sub == "u-1"
        verified = svc.verify(token)
        assert verified.sub == "u-1"

    def test_expired_token_rejected(self):
        import time
        svc = AuthService(secret="test-secret", expire_hours=-1)  # already expired
        token, _ = svc.login(user_id="u-1", role="admin",
                              stored_hash=svc.hash_password("pw"))
        with pytest.raises(Exception):
            svc.verify(token)

    def test_wrong_password_same_error(self):
        svc = AuthService(secret="s")
        with pytest.raises(Exception) as e1:
            svc.login(user_id="nonexist", role="admin", stored_hash="x", password="wrong")
        # Error message should not reveal whether user exists
```

```python
# platform/tests/unit/test_rbac.py
from rsi.security.rbac import ROLE_PERMISSIONS

class TestRbacMatrix:
    def test_admin_has_all(self):
        assert "sut:manage" in ROLE_PERMISSIONS["admin"]
        assert "credential:manage" in ROLE_PERMISSIONS["admin"]
        assert "approval:decide" in ROLE_PERMISSIONS["admin"]

    def test_engineer_no_credentials(self):
        assert "credential:manage" not in ROLE_PERMISSIONS["agent_engineer"]

    def test_researcher_readonly(self):
        perms = ROLE_PERMISSIONS["researcher"]
        assert "view:read" in perms
        assert "sut:manage" not in perms
```

- [ ] **Step 2: Write auth.py + rbac.py**

Follow DD-03 §4.1-4.2. Auth: PyJWT HS256, argon2-cffi PasswordHasher. RBAC: dict constant + `require()` factory returning FastAPI dependency.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_auth.py tests/unit/test_rbac.py -v
git add platform/rsi/security/ platform/tests/unit/test_auth.py platform/tests/unit/test_rbac.py
git commit -m "feat: JWT auth + RBAC permission matrix"
```

---

### Task 7: Security — Credentials + Audit + Hygiene

**Files:**
- Create: `platform/rsi/security/credentials.py`
- Create: `platform/rsi/security/audit.py`
- Create: `platform/rsi/security/hygiene.py`
- Test: `platform/tests/unit/test_credentials.py`
- Test: `platform/tests/unit/test_audit.py`

**Interfaces:**
- Produces: `CredentialStore` (Fernet put/resolve/rotate/fingerprint), `CredentialResolver` (ssh_access→ResolvedCreds), `AuditLog` (@audited decorator + record()), `SecretHygiene` (startup_check + log filter)

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_credentials.py
import pytest
from rsi.security.credentials import CredentialStore, CredentialResolver
from rsi.core.models import EnvSpec, DeviceSpec, SshAccess

class TestCredentialStore:
    def test_roundtrip(self, tmp_path):
        store = CredentialStore(master_key="dGVzdC1rZXktMzItYnl0ZXMtMTIzNDU2Nzg5MCE=")  # base64 32 bytes
        cred_id = store.put("my-cred", "device_ssh", "secret123")
        assert store.resolve("my-cred") == "secret123"

    def test_rotate(self):
        store = CredentialStore(master_key="dGVzdC1rZXktMzItYnl0ZXMtMTIzNDU2Nzg5MCE=")
        store.put("my-cred", "device_ssh", "old")
        store.rotate(store.resolve_id("my-cred"), "new")
        assert store.resolve("my-cred") == "new"

    def test_fingerprint(self):
        store = CredentialStore(master_key="dGVzdC1rZXktMzItYnl0ZXMtMTIzNDU2Nzg5MCE=")
        store.put("my-cred", "device_ssh", "secret123")
        fp = store.fingerprints_of(store.resolve_id("my-cred"))
        assert len(fp.patterns) == 2  # raw + base64
        assert "secret123" in fp.patterns

class TestCredentialResolver:
    def test_resolve_env(self):
        env = EnvSpec(
            name="t", backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="c")],
            topology={"links": []},
            ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password="pw")},
        )
        resolver = CredentialResolver(store=None)
        creds = resolver.resolve_env(env, permission="read-only")
        assert "R1" in creds.device_env
        assert creds.device_env["R1"]["DEV_R1_HOST"] == "10.0.0.1"
        assert creds.device_env["R1"]["DEV_R1_PASS"] == "pw"
```

- [ ] **Step 2: Write credentials.py + audit.py + hygiene.py**

Follow DD-03 §4. Audit: `@audited(action)` decorator wrapping function, extracting actor from context, writing to audit_log table. Hygiene: check `RSI_MASTER_KEY` and `RSI_JWT_SECRET` exist and ≥32 bytes.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_credentials.py tests/unit/test_audit.py -v
git add platform/rsi/security/ platform/tests/unit/test_cred*.py platform/tests/unit/test_audit.py
git commit -m "feat: CredentialStore + AuditLog + SecretHygiene"
```

---

### Task 8: Versioning — Changeset + Worktrees

**Files:**
- Create: `platform/rsi/versioning/__init__.py`
- Create: `platform/rsi/versioning/changeset.py`
- Create: `platform/rsi/versioning/worktrees.py`
- Test: `platform/tests/unit/test_changeset.py`
- Test: `platform/tests/unit/test_worktrees.py`

**Interfaces:**
- Produces: `Changeset`, `FileChange` data classes; `_Worktrees` class (create_temp/create_staging/cleanup/list_orphans)

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_worktrees.py
import pytest
from pathlib import Path
from rsi.versioning.worktrees import Worktrees

class TestWorktrees:
    def test_staging_persists(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        staging = wt.staging()
        assert staging.exists()
        assert (staging / ".git").exists()  # it's a worktree

    def test_temp_lifecycle(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        temp = wt.temp("op-123")
        assert temp.exists()
        wt.cleanup("op-123")
        assert not temp.exists()
```

- [ ] **Step 2: Write changeset.py + worktrees.py**

Changeset: Pydantic model with `files: list[FileChange]`, each has `path/op/content/target/declared`. Worktrees: GitPython worktree management wrapping `git worktree add/remove`.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_changeset.py tests/unit/test_worktrees.py -v
git add platform/rsi/versioning/ platform/tests/unit/test_changeset.py platform/tests/unit/test_worktrees.py
git commit -m "feat: Changeset data structure + worktree lifecycle management"
```

---

### Task 9: Versioning — GitStore Core (snapshot/commit/promote/rollback)

**Files:**
- Create: `platform/rsi/versioning/gitstore.py`
- Test: `platform/tests/unit/test_gitstore.py`

**Interfaces:**
- Consumes: `Worktrees` from Task 8, `Changeset` from Task 8
- Produces: `GitStore` class with `init()`, `snapshot(sut_id)`, `commit_candidate(base, cs, hyp_id)`, `promote(candidate)`, `rollback(sut_id, version_id)`, `_write_lock`

- [ ] **Step 1: Write failing tests (uses real temp git repo)**

```python
# platform/tests/unit/test_gitstore.py
import pytest
from pathlib import Path
from rsi.versioning.gitstore import GitStore
from rsi.versioning.changeset import Changeset, FileChange

@pytest.fixture
def gitstore(tmp_path):
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
    )
    store.init()
    # Seed: create a SUT directory and initial baseline
    staging = store.worktrees.staging()
    sut_dir = staging / "suts" / "test-agent"
    sut_dir.mkdir(parents=True)
    (sut_dir / "system-prompt.md").write_text("# Initial prompt")
    store._commit_staging("Initial baseline")
    store._tag_baseline("test-agent", "v1")
    return store

class TestGitStore:
    def test_snapshot_idempotent(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        v2 = gitstore.snapshot("test-agent")  # no changes
        assert v1 == v2  # same commit

    def test_commit_candidate(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = Changeset(
            hypothesis_id="hyp-001",
            base_version=base,
            files=[FileChange(path="suts/test-agent/system-prompt.md",
                            op="edit", content="# Updated prompt",
                            target="system_prompt", declared=True)],
        )
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        assert cand != base

    def test_promote_fast_forward(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = Changeset(hypothesis_id="hyp-001", base_version=base,
                      files=[FileChange(path="suts/test-agent/system-prompt.md",
                                      op="edit", content="# V2", target="system_prompt", declared=True)])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        gitstore.promote(cand)  # should not raise

    def test_rollback_keeps_history(self, gitstore):
        base = gitstore.snapshot("test-agent")
        gitstore.rollback("test-agent", base)
        # Old tag should still exist (keep-all)
```

- [ ] **Step 2: Write gitstore.py**

Follow DD-09 §4. Key methods using GitPython: `_commit_staging` (add+commit on staging worktree), `snapshot` (check diff → commit if changed → return VersionId), `commit_candidate` (create temp worktree at base → apply changeset files → commit → create branch `cand/<hyp_id>` → cleanup), `promote` (check fast-forward → create tag `baseline/v<N+1>`), `rollback` (create tag pointing to historical commit). All mutating ops guarded by `asyncio.Lock`.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_gitstore.py -v
git add platform/rsi/versioning/gitstore.py platform/tests/unit/test_gitstore.py
git commit -m "feat: GitStore core (snapshot/commit_candidate/promote/rollback with keep-all lineage)"
```

---

### Task 10: Versioning — Export/Diff/Freeze/Files API

**Files:**
- Modify: `platform/rsi/versioning/gitstore.py` (add remaining methods)
- Create: `platform/rsi/versioning/freeze.py`
- Create: `platform/rsi/versioning/hooks.py`
- Test: `platform/tests/unit/test_gitstore_extras.py`

**Interfaces:**
- Produces: `export()`, `diff()`, `lineage()`, `freeze_evalset()`, `verify_freeze()`, `list_files()`, `write_file()` on GitStore; `install_hooks()` on `_Hooks`

- [ ] **Step 1: Write failing tests**

```python
# platform/tests/unit/test_gitstore_extras.py
class TestGitStoreExtras:
    def test_export(self, gitstore):
        base = gitstore.snapshot("test-agent")
        dest = gitstore.export(base, Path(tmp_path) / "export")
        assert (dest / "suts" / "test-agent" / "system-prompt.md").exists()

    def test_diff(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        # ... create v2 with change
        diffs = gitstore.diff(v1, v2)
        assert any("system-prompt" in d.path for d in diffs)

    def test_freeze_evalset(self, gitstore, tmp_path):
        # Create evalset files in repo
        staging = gitstore.worktrees.staging()
        es_dir = staging / "evalsets" / "test-es"
        es_dir.mkdir(parents=True, exist_ok=True)
        (es_dir / "env.yaml").write_text("name: test")
        gitstore._commit_staging("add evalset")
        record = gitstore.freeze_evalset("test-es")
        assert record.content_hash is not None

    def test_verify_freeze_ok(self, gitstore):
        assert gitstore.verify_freeze() == True  # or handles no freeze manifest
```

- [ ] **Step 2: Implement remaining GitStore methods + freeze.py + hooks.py**

Export: temp worktree → copytree → cleanup. Diff: GitPython `commit.diff(other_commit)`. Freeze: compute sha256 of each file in evalset dir → manifest JSON → tag. Hooks: write pre-receive bash script to bare repo hooks/ dir.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_gitstore_extras.py -v
git add platform/rsi/versioning/ platform/tests/unit/test_gitstore_extras.py
git commit -m "feat: GitStore full API (export/diff/lineage/freeze/verify/files CRUD)"
```

---

### Task 11: Integration Test — Foundation Smoke

**Files:**
- Test: `platform/tests/integration/test_foundation.py`

**Interfaces:**
- Consumes: All previous tasks
- Produces: Confidence that the entire foundation works together

- [ ] **Step 1: Write integration test**

```python
# platform/tests/integration/test_foundation.py
"""Foundation smoke test: DB → GitStore → Security → Models round-trip."""
import pytest

@pytest.mark.asyncio
async def test_full_foundation(tmp_path):
    # 1. Init DB
    # 2. Init GitStore
    # 3. Create a SUT with config files
    # 4. Snapshot → commit candidate → promote → verify lineage
    # 5. Create an evalset → freeze → verify
    # 6. Create user → login → verify JWT
    # 7. Audit trail exists
    ...
```

- [ ] **Step 2: Run and commit**

```bash
uv run pytest tests/integration/test_foundation.py -v
git add platform/tests/integration/test_foundation.py
git commit -m "test: foundation integration smoke test (DB+Git+Security+Models)"
```

---

## Self-Review Notes

- All 11 tasks follow TDD: test first → implement → test passes → commit
- Each task is independently testable and reviewable
- Cross-task interfaces clearly documented in "Interfaces" blocks
- Full specifications referenced from DD-01/03/09 — plan shows structure + key test cases, not every field
- Total estimated tasks: 11 (manageable for one execution session or subagent batch)
