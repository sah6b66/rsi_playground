"""GitStore facade: snapshot / commit_candidate / promote / rollback (DD-09 §4-5).

A single bare repository holds all objects. A persistent *staging* worktree
is the source of truth for manual edits; per-operation temp worktrees are
created from arbitrary commits and always cleaned up (try/finally).

Refs layout::

    suts/<sut_id>/baseline/v<N>    # tags; N only increments (keep-all lineage)
    suts/<sut_id>/cand/<hyp_id>    # branches pointing at candidate commits
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import git

from rsi.core.errors import GitStoreError, NotFastForwardError
from rsi.log import get_logger
from rsi.versioning.hooks import install_hooks
from rsi.versioning.changeset import (
    Changeset,
    FileChange,
    FileDiff,
    FreezeRecord,
    LineageGraph,
    LineageNode,
    VersionId,
)
from rsi.versioning.worktrees import Worktrees

_BASELINE_RE = re.compile(r"^suts/(?P<sut>[^/]+)/baseline/v(?P<n>\d+)$")
_CAND_RE = re.compile(r"^suts/(?P<sut>[^/]+)/cand/(?P<hyp>[^/]+)$")
_SUT_PATH_RE = re.compile(r"^suts/(?P<sut>[^/]+)/")
_FREEZE_HASH_RE = re.compile(r"content_hash=([0-9a-f]{64})")
_FREEZE_TS_RE = re.compile(r"frozen_at=(\S+)")

logger = get_logger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _baseline_prefix(sut_id: str) -> str:
    return f"suts/{sut_id}/baseline/"


class GitStore:
    """Git-backed artifact store for SUT versions (FR6.5/FR8)."""

    def __init__(
        self,
        repo_path: Path,
        worktree_root: Path,
        session_factory=None,
        audit_log=None,
        target_registry=None,
    ) -> None:
        """Build the GitStore facade.

        ``session_factory`` / ``audit_log`` are optional DB integration hooks
        (DD-09 §4): when supplied, successful version operations also register
        rows in ``config_versions`` / ``suts`` / ``evalsets`` and append audit
        entries. ``target_registry`` (rsi.optimizer.targets.TargetRegistry)
        enables path-domain validation on ``commit_candidate`` and
        ``write_file(commit=True)``.

        Drift risk (accepted): all DB/audit writes are wrapped in try/except
        and only emit a warning on failure — a DB outage never rolls back the
        already-completed Git operation, so Git and DB can temporarily drift
        until the next successful operation / reconciliation.
        """
        self.repo_path = Path(repo_path)
        self.worktree_root = Path(worktree_root)
        self.repo: git.Repo | None = None
        self.worktrees: Worktrees | None = None
        self.session_factory = session_factory
        self.audit_log = audit_log
        self.target_registry = target_registry
        # Serialize all mutating ops (snapshot/commit/promote/rollback).
        # Sync methods -> threading.Lock instead of asyncio.Lock (DD-09 §4).
        self._write_lock = threading.RLock()

    # ── lifecycle ────────────────────────────────────────────────────

    def init(self) -> None:
        """Initialize the bare repo + worktrees + staging worktree."""
        with self._write_lock:
            if self.repo is not None:
                return
            try:
                if self.repo_path.exists() and (self.repo_path / "HEAD").exists():
                    repo = git.Repo(self.repo_path)
                else:
                    repo = git.Repo.init(self.repo_path, bare=True)
            except git.GitCommandError as exc:  # pragma: no cover
                raise GitStoreError(f"repo init failed: {exc}") from exc
            self.repo = repo
            self.worktrees = Worktrees(self.worktree_root, repo=repo)
            # Touch staging so it exists from the start.
            self.worktrees.staging()
            # Server-side guard rails (DD-09 §5.2).
            install_hooks(self.repo_path)

    def _require(self) -> tuple[git.Repo, Worktrees, Path]:
        if self.repo is None or self.worktrees is None:
            raise GitStoreError("GitStore not initialized; call init() first")
        return self.repo, self.worktrees, self.worktrees.staging()

    def _staging_repo(self, staging: Path) -> git.Repo:
        return git.Repo(staging)

    # ── version operations ───────────────────────────────────────────

    def snapshot(self, sut_id: str) -> VersionId:
        """Commit ``suts/<sut_id>/`` from staging; idempotent when clean.

        Returns the current baseline tag commit when nothing changed.
        """
        _, _, staging = self._require()
        with self._write_lock:
            srepo = self._staging_repo(staging)
            rel = f"suts/{sut_id}"
            status = srepo.git.status("--porcelain", "--", rel)
            if not status.strip():
                tag = self._get_baseline_tag(sut_id)
                if tag is not None:
                    return VersionId(tag.commit.hexsha)
                return VersionId(srepo.head.commit.hexsha)
            vid = self._commit_staging(f"snapshot: {sut_id}", paths=[rel])
            self._register_version(vid, sut_id, "baseline", f"snapshot {sut_id}")
            return vid

    def commit_candidate(self, base: VersionId, cs: Changeset, hyp_id: str) -> VersionId:
        """Apply ``cs`` on top of ``base`` in a temp worktree, commit, branch."""
        repo, worktrees, _ = self._require()
        self._validate_paths([fc.path for fc in cs.files])
        sut_id = self._sut_from_changeset(cs)
        op_id = f"tmp-{uuid.uuid4().hex}"
        with self._write_lock:
            path = worktrees.worktree_root / op_id
            wrepo = None
            try:
                repo.git.worktree("add", str(path), str(base))
                wrepo = git.Repo(path)
                self._apply_changes(wrepo.working_tree_dir, cs.files)
                wrepo.git.add("-A")
                wrepo.index.commit(f"cand: {sut_id}/{hyp_id}")
                sha = wrepo.head.commit.hexsha
                branch = f"suts/{sut_id}/cand/{hyp_id}"
                if branch in [h.name for h in repo.heads]:
                    repo.delete_head(branch, force=True)
                repo.create_head(branch, sha)
                vid = VersionId(sha)
                self._register_version(vid, sut_id, "candidate", hyp_id)
                return vid
            except git.GitCommandError as exc:
                raise GitStoreError(
                    f"commit_candidate failed: {exc}",
                    sut_id=sut_id, hyp_id=hyp_id,
                ) from exc
            finally:
                if wrepo is not None:
                    wrepo.close()
                try:
                    worktrees.cleanup(op_id)
                except git.GitCommandError:
                    # Windows: open handles can block `worktree remove`;
                    # fall back to rmtree + prune.
                    shutil.rmtree(path, ignore_errors=True)
                    repo.git.worktree("prune")

    def promote(self, candidate: VersionId) -> None:
        """Fast-forward the baseline tag to ``candidate`` and sync staging."""
        repo, _, staging = self._require()
        sut_id = self._sut_of_candidate(candidate)
        with self._write_lock:
            cand_commit = repo.commit(str(candidate))
            tag = self._get_baseline_tag(sut_id)
            if tag is not None and not repo.is_ancestor(tag.commit, cand_commit):
                raise NotFastForwardError(
                    f"candidate {candidate[:8]} is not a descendant of "
                    f"baseline {tag.name}",
                    sut_id=sut_id, candidate=str(candidate), baseline=tag.name,
                )
            name = _baseline_prefix(sut_id) + f"v{self._next_tag_num(sut_id)}"
            repo.create_tag(name, ref=cand_commit, message=f"promote {sut_id}")
            self._sync_staging(staging, cand_commit.hexsha)
            self._db_call(lambda: self._update_sut_baseline(sut_id, str(candidate)))
            self._audit(
                "version.promote", sut_id,
                sut_id=sut_id, candidate=str(candidate), tag=name,
            )

    def rollback(self, sut_id: str, version_id: VersionId) -> None:
        """New baseline tag at a historical commit (keep-all: old tags stay)."""
        repo, _, staging = self._require()
        with self._write_lock:
            commit = repo.commit(str(version_id))
            name = _baseline_prefix(sut_id) + f"v{self._next_tag_num(sut_id)}"
            repo.create_tag(name, ref=commit, message=f"rollback {sut_id}")
            self._sync_staging(staging, commit.hexsha)
            self._audit(
                "version.rollback", sut_id,
                sut_id=sut_id, version=str(version_id), tag=name,
            )

    # ── export / diff / lineage (read-only, no lock) ─────────────────

    def export(self, version_id: VersionId, dest: Path) -> Path:
        """Copy the full file tree at ``version_id`` into ``dest`` via a temp worktree."""
        repo, worktrees, _ = self._require()
        dest = Path(dest)
        op_id = f"tmp-{uuid.uuid4().hex}"
        path = worktrees.worktree_root / op_id
        try:
            repo.git.worktree("add", str(path), str(version_id))
            shutil.copytree(
                path,
                dest,
                ignore=shutil.ignore_patterns(".git"),
                dirs_exist_ok=True,
            )
        except git.GitCommandError as exc:
            raise GitStoreError(
                f"export failed: {exc}", version_id=str(version_id), dest=str(dest)
            ) from exc
        finally:
            try:
                worktrees.cleanup(op_id)
            except git.GitCommandError:
                shutil.rmtree(path, ignore_errors=True)
                repo.git.worktree("prune")
        return dest

    def diff(self, a: VersionId, b: VersionId) -> list[FileDiff]:
        """File-level diff between two commits."""
        repo, _, _ = self._require()
        try:
            commit_a = repo.commit(str(a))
            commit_b = repo.commit(str(b))
        except git.GitCommandError as exc:
            raise GitStoreError(f"diff failed: {exc}", a=str(a), b=str(b)) from exc
        out: list[FileDiff] = []
        for d in commit_a.diff(commit_b, create_patch=True):
            if d.new_file:
                op, path = "create", str(d.b_path)
            elif d.deleted_file:
                op, path = "delete", str(d.a_path)
            else:
                op, path = "edit", str(d.b_path)
            patch = d.diff
            if isinstance(patch, bytes):
                patch = patch.decode("utf-8", "replace")
            out.append(FileDiff(path=path, op=op, patch=patch or ""))
        return out

    def lineage(self, sut_id: str) -> LineageGraph:
        """Graph of baseline tags + candidate branches for ``sut_id``."""
        repo, _, _ = self._require()
        # Key nodes by label: a tag and a branch may point at the same commit.
        nodes: dict[str, LineageNode] = {}
        shas: set[str] = set()
        edges: list[tuple[str, str]] = []
        for tag in repo.tags:
            m = _BASELINE_RE.match(tag.name)
            if not m or m.group("sut") != sut_id:
                continue
            sha = tag.commit.hexsha
            parents = [p.hexsha for p in tag.commit.parents]
            nodes[tag.name] = LineageNode(
                version_id=sha, label=tag.name, kind="baseline", parent_ids=parents
            )
            shas.add(sha)
        for head in repo.heads:
            m = _CAND_RE.match(head.name)
            if not m or m.group("sut") != sut_id:
                continue
            sha = head.commit.hexsha
            parents = [p.hexsha for p in head.commit.parents]
            if head.name not in nodes:
                nodes[head.name] = LineageNode(
                    version_id=sha,
                    label=head.name,
                    kind="candidate",
                    parent_ids=parents,
                    experiment_ids=[m.group("hyp")],
                )
                shas.add(sha)
        for node in nodes.values():
            for p in node.parent_ids:
                if p in shas:
                    edges.append((p, node.version_id))
        return LineageGraph(nodes=list(nodes.values()), edges=edges)

    # ── evalset freeze (FR3.3) ───────────────────────────────────────

    def freeze_evalset(self, evalset_id: str) -> FreezeRecord:
        """Canonical-hash + tag ``evalsets/<id>/freeze/v<N>`` (idempotent per content).

        Re-freezing unchanged content reuses the existing freeze tag instead
        of creating a new one; changed content creates the next tag.
        """
        repo, _, staging = self._require()
        with self._write_lock:
            es_dir = staging / "evalsets" / evalset_id
            if not es_dir.is_dir():
                raise GitStoreError(
                    f"evalset directory missing: evalsets/{evalset_id}",
                    evalset_id=evalset_id,
                )
            h = hashlib.sha256()
            for f in sorted(es_dir.rglob("*")):
                if f.is_file():
                    h.update(f.read_bytes())
            content_hash = h.hexdigest()
            srepo = self._staging_repo(staging)
            sha = srepo.head.commit.hexsha
            prefix = f"evalsets/{evalset_id}/freeze/"
            n = 1
            for tag in repo.tags:
                if not tag.name.startswith(prefix):
                    continue
                m = re.match(rf"{re.escape(prefix)}v(\d+)$", tag.name)
                if m:
                    n = max(n, int(m.group(1)) + 1)
                # Idempotency: reuse the existing tag for identical content.
                msg = ""
                try:
                    msg = tag.tag.message or ""
                except Exception:  # noqa: BLE001 - lightweight tag: no message
                    msg = ""
                mh = _FREEZE_HASH_RE.search(msg)
                if mh and mh.group(1) == content_hash:
                    record = FreezeRecord(
                        evalset_id=evalset_id,
                        git_ref=tag.commit.hexsha,
                        content_hash=content_hash,
                        frozen_at=self._freeze_ts(tag, msg),
                    )
                    self._db_call(
                        lambda: self._update_evalset_freeze(
                            evalset_id, record.git_ref,
                            record.content_hash, record.frozen_at,
                        )
                    )
                    return record
            name = f"{prefix}v{n}"
            frozen_at = _utcnow_iso()
            repo.create_tag(
                name,
                ref=sha,
                message=(
                    f"freeze {evalset_id} "
                    f"content_hash={content_hash} frozen_at={frozen_at}"
                ),
            )
            self._db_call(
                lambda: self._update_evalset_freeze(
                    evalset_id, sha, content_hash, frozen_at
                )
            )
            self._audit(
                "evalset.freeze", evalset_id,
                evalset_id=evalset_id, tag=name, content_hash=content_hash,
            )
            return FreezeRecord(
                evalset_id=evalset_id,
                git_ref=sha,
                content_hash=content_hash,
                frozen_at=frozen_at,
            )

    def verify_freeze(self) -> bool:
        """Check ``judge/freeze.manifest`` (path → sha256) against staging files."""
        _, _, staging = self._require()
        manifest_path = staging / "judge" / "freeze.manifest"
        if not manifest_path.is_file():
            return True  # no freeze configured = OK
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        for rel, expected in manifest.items():
            f = staging / rel
            if not f.is_file():
                return False
            if hashlib.sha256(f.read_bytes()).hexdigest() != expected:
                return False
        return True

    # ── manual edit API (FR10.5 P0) ──────────────────────────────────

    def list_files(self, sut_id: str, version: VersionId | None = None) -> dict[str, str]:
        """Return {relpath: content} for suts/<sut_id>/ at a version or staging."""
        repo, worktrees, staging = self._require()
        if version is None:
            return self._read_sut_dir(staging / "suts" / sut_id, staging)
        op_id = f"tmp-{uuid.uuid4().hex}"
        path = worktrees.worktree_root / op_id
        try:
            repo.git.worktree("add", str(path), str(version))
            return self._read_sut_dir(path / "suts" / sut_id, path)
        except git.GitCommandError as exc:
            raise GitStoreError(
                f"list_files failed: {exc}", sut_id=sut_id, version=str(version)
            ) from exc
        finally:
            try:
                worktrees.cleanup(op_id)
            except git.GitCommandError:
                shutil.rmtree(path, ignore_errors=True)
                repo.git.worktree("prune")

    def write_file(
        self,
        sut_id: str,
        path: str,
        content: str,
        commit: bool = False,
        message: str = "",
    ) -> None:
        """Write ``suts/<sut_id>/<path>`` in staging; optionally commit."""
        _, _, staging = self._require()
        rel = f"suts/{sut_id}/{path}"
        if commit:
            self._validate_paths([rel])
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if commit:
            with self._write_lock:
                self._commit_staging(message or f"write: {rel}", paths=[f"suts/{sut_id}"])

    def delete_file(
        self,
        sut_id: str,
        path: str,
        commit: bool = False,
        message: str = "",
    ) -> None:
        """Delete ``suts/<sut_id>/<path>`` from staging; optionally commit.

        Staging-only delete (``commit=False``) removes the file from the
        staging worktree without creating a version.
        """
        _, _, staging = self._require()
        rel = f"suts/{sut_id}/{path}"
        target = staging / rel
        if target.is_file():
            target.unlink()
        if commit:
            with self._write_lock:
                self._commit_staging(message or f"delete: {rel}", paths=[f"suts/{sut_id}"])

    # ── internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _read_sut_dir(sut_dir: Path, root: Path) -> dict[str, str]:
        if not sut_dir.is_dir():
            raise GitStoreError(f"directory missing: {sut_dir.relative_to(root)}")
        return {
            str(f.relative_to(sut_dir)).replace("\\", "/"): f.read_text(encoding="utf-8")
            for f in sorted(sut_dir.rglob("*"))
            if f.is_file()
        }

    def _commit_staging(self, message: str, paths: list[str] | None = None) -> VersionId:
        """Add (+ optionally path-filter) and commit on staging; return sha."""
        _, _, staging = self._require()
        srepo = self._staging_repo(staging)
        if paths:
            srepo.git.add("--", *paths)
        else:
            srepo.git.add("-A")
        if not srepo.index.diff("HEAD") and not srepo.git.status("--porcelain").strip():
            return VersionId(srepo.head.commit.hexsha)
        commit = srepo.index.commit(message)
        return VersionId(commit.hexsha)

    def _tag_baseline(self, sut_id: str, tag_short: str) -> None:
        """Create baseline tag ``suts/<sut>/baseline/<tag_short>`` at staging HEAD."""
        _, _, staging = self._require()
        srepo = self._staging_repo(staging)
        self.repo.create_tag(
            _baseline_prefix(sut_id) + tag_short,
            ref=srepo.head.commit,
            message=f"baseline {sut_id} {tag_short}",
        )

    def _get_baseline_tag(self, sut_id: str) -> git.TagReference | None:
        """Latest ``v<N>`` baseline tag for ``sut_id`` (max N), or None."""
        best: git.TagReference | None = None
        best_n = -1
        for tag in self.repo.tags:
            m = _BASELINE_RE.match(tag.name)
            if m and m.group("sut") == sut_id:
                n = int(m.group("n"))
                if n > best_n:
                    best, best_n = tag, n
        return best

    def _next_tag_num(self, sut_id: str) -> int:
        tag = self._get_baseline_tag(sut_id)
        return (int(_BASELINE_RE.match(tag.name).group("n")) + 1) if tag else 1

    def _sync_staging(self, staging: Path, sha: str) -> None:
        """Hard-reset the staging worktree (index + working tree) to ``sha``."""
        srepo = self._staging_repo(staging)
        srepo.git.reset("--hard", sha)

    def _sut_from_changeset(self, cs: Changeset) -> str:
        if not cs.files:
            raise GitStoreError("changeset has no file changes", hyp_id=cs.hypothesis_id)
        for fc in cs.files:
            m = _SUT_PATH_RE.match(fc.path)
            if m:
                return m.group("sut")
        raise GitStoreError(
            "cannot derive sut_id from changeset paths "
            "(expected 'suts/<sut_id>/...')",
            hyp_id=cs.hypothesis_id,
        )

    def _sut_of_candidate(self, candidate: VersionId) -> str:
        """Derive sut_id from the candidate branch pointing at ``candidate``."""
        for head in self.repo.heads:
            m = re.match(r"^suts/(?P<sut>[^/]+)/cand/", head.name)
            if m and head.commit.hexsha == str(candidate):
                return m.group("sut")
        raise GitStoreError(
            f"no candidate branch suts/<sut>/cand/* points at {candidate}",
            candidate=str(candidate),
        )

    def _apply_changes(self, tree_dir: str | Path, files: list[FileChange]) -> None:
        for fc in files:
            target = Path(tree_dir) / fc.path
            if fc.op == "delete":
                if target.exists():
                    target.unlink()
            else:
                if fc.content is None:
                    raise GitStoreError(
                        f"FileChange op={fc.op!r} requires content",
                        path=fc.path,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fc.content, encoding="utf-8")

    # ── DB registration / audit (A-2) ────────────────────────────────

    def _db_call(self, fn) -> None:
        """Run a DB write; failure logs a warning but never raises.

        Drift risk: the Git operation has already succeeded at this point,
        so a DB failure leaves Git ahead of the DB until reconciliation.
        """
        if self.session_factory is None:
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - deliberate containment
            logger.warning("gitstore.db_write_failed", error=str(exc))

    def _audit(self, action: str, object_id: str, /, **detail) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(
                actor="system",
                action=action,
                object_type=action.split(".", 1)[0],
                object_id=object_id,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate containment
            logger.warning("gitstore.audit_write_failed", error=str(exc))

    def _register_version(
        self, version_id: VersionId, sut_id: str, kind: str, label: str
    ) -> None:
        if self.session_factory is None:
            return
        from rsi.db.tables import ConfigVersion

        def _do() -> None:
            with self.session_factory() as session:  # type: ignore[misc]
                session.add(
                    ConfigVersion(
                        id=str(version_id),
                        sut_id=sut_id,
                        git_ref=str(version_id),
                        label=label,
                        kind=kind,
                        created_at=_utcnow_iso(),
                        created_by="system",
                    )
                )
                session.commit()

        self._db_call(_do)

    def _update_sut_baseline(self, sut_id: str, version_id: str) -> None:
        from rsi.db.tables import Sut

        with self.session_factory() as session:  # type: ignore[misc]
            row = session.get(Sut, sut_id)
            if row is None:
                logger.warning(
                    "gitstore.sut_row_missing", sut_id=sut_id,
                )
                return
            row.baseline_version_id = version_id
            session.add(row)
            session.commit()

    def _update_evalset_freeze(
        self, evalset_id: str, git_ref: str, content_hash: str, frozen_at: str
    ) -> None:
        from rsi.db.tables import EvalSet

        with self.session_factory() as session:  # type: ignore[misc]
            row = session.get(EvalSet, evalset_id)
            if row is None:
                logger.warning(
                    "gitstore.evalset_row_missing", evalset_id=evalset_id,
                )
                return
            row.git_ref = git_ref
            row.content_hash = content_hash
            row.frozen_at = frozen_at
            session.add(row)
            session.commit()

    @staticmethod
    def _freeze_ts(tag, msg: str) -> str:
        m = _FREEZE_TS_RE.search(msg)
        if m:
            return m.group(1)
        try:
            return datetime.fromtimestamp(
                tag.tag.tagged_date, tz=timezone.utc
            ).isoformat()
        except Exception:  # noqa: BLE001 - fall back to now
            return _utcnow_iso()

    # ── path-domain validation (A-8) ─────────────────────────────────

    def _validate_paths(self, paths: list[str]) -> None:
        """TargetRegistry whitelist + frozen-domain check (DD-09 §5.2)."""
        if self.target_registry is None:
            return
        enabled = set(self.target_registry.TARGET_PATHS)
        for p in paths:
            self.target_registry.validate(p, enabled)
