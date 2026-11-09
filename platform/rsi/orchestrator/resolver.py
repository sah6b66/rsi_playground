"""Production batch_resolver — API batch dict -> real run_batch inputs.

D-10/N-2 (audit4): the default assembly used to run every ``POST /batches``
on a canned fake env + one ad-hoc task. This resolver loads the real
evalset payload (env.yaml + tasks.jsonl semantics as stored by the evalsets
router's ``:import`` — ``env_yaml`` text, ``tasks`` list-of-dicts,
``configs`` mapping) and the SUT's ``image_ref``.

Data-source priority per record type:

* evalset: in-memory ``evalsets`` store (payload lives here in P0; the DB
  ``evalsets`` table only proves existence — metadata, no task rows are
  synced to it) → DB row (existence only → honest degradation, see below)
  → ``NotFoundError``.
* sut/image_ref: in-memory ``suts`` store → DB ``suts`` row → ``sut_id``
  fallback.

Honest degradation: an evalset record without env/tasks payload (e.g.
created via ``POST /evalsets`` rather than ``:import``) resolves to the
same canned default as the offline fallback, but marks the batch params
``env_source: "default_offline"`` so downstream consumers can tell real
from synthesized inputs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from rsi.core.errors import NotFoundError, ValidationError
from rsi.core.models import (
    ExpectedAnswer,
    TaskRecord,
    TaskSpec,
)
from rsi.log import get_logger
from rsi.orchestrator.batch import default_trigger_inputs

__all__ = ["build_batch_resolver", "build_noise_run_batch_fn"]

logger = get_logger(__name__)


def _db_evalset_exists(session_factory, evalset_id: str) -> bool:
    if session_factory is None:
        return False
    try:
        from rsi.db.tables import EvalSet

        with session_factory() as session:
            return session.get(EvalSet, evalset_id) is not None
    except Exception:  # noqa: BLE001 - DB is best-effort existence proof
        return False


def _db_image_ref(session_factory, sut_id: str) -> str | None:
    if session_factory is None:
        return None
    try:
        from rsi.db.tables import Sut

        with session_factory() as session:
            row = session.get(Sut, sut_id)
            return str(row.image_ref) if row is not None else None
    except Exception:  # noqa: BLE001
        return None


def _parse_env(env_yaml: str, evalset_id: str):
    """env.yaml text -> EnvSpec; None when no yaml is available."""
    if not str(env_yaml or "").strip():
        return None
    try:
        data = yaml.safe_load(env_yaml)
        from rsi.core.models import EnvSpec

        return EnvSpec.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - surfaced as batch failure
        raise ValidationError(
            f"evalset {evalset_id!r} env.yaml is invalid: {exc}",
            context={"evalset_id": evalset_id},
        ) from exc


def _parse_tasks(raw_tasks: list[dict], evalset_id: str) -> list[TaskRecord] | None:
    """tasks.jsonl rows -> TaskRecord list; None when no rows are available.

    Row contract (mirrors the ``:import`` zip format): ``id`` (TaskRecord
    task_id), ``prompt`` (TaskSpec.prompt); optional ``title``/``prepare``/
    ``expected`` mapped onto the TaskRecord fields. A bad row fails the
    whole resolution with the offending line number listed.
    """
    if not raw_tasks:
        return None
    records: list[TaskRecord] = []
    for i, t in enumerate(raw_tasks):
        try:
            records.append(
                TaskRecord(
                    task_id=str(t["id"]),
                    title=str(t.get("title") or t["id"]),
                    prepare=list(t.get("prepare") or []),
                    task=TaskSpec(prompt=str(t.get("prompt", ""))),
                    expected=(
                        ExpectedAnswer.model_validate(t["expected"])
                        if t.get("expected") is not None
                        else ExpectedAnswer()
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - aggregated below
            raise ValidationError(
                f"evalset {evalset_id!r} tasks.jsonl line {i + 1} is invalid: {exc}",
                context={"evalset_id": evalset_id, "line": i + 1},
            ) from exc
    return records


def _resolve_config_dir(gitstore, version_id: str) -> str:
    """config_version_id -> exportable config directory.

    Pass-through when the id is already a live directory (P0 path-as-id);
    GitStore-registered versions are exported to a temp worktree copy so
    the instrumentor/config overlay can consume them. Anything unknown
    (including an uninitialized GitStore) degrades to ``""`` — the
    documented empty-id behavior (never resolves to cwd).
    """
    vid = str(version_id or "").strip()
    if not vid:
        return ""
    if Path(vid).is_dir():
        return vid
    if gitstore is None:
        return ""
    try:
        dest = Path(tempfile.mkdtemp(prefix="rsi-resolver-cfg-"))
        gitstore.export(vid, dest)
        return str(dest)
    except Exception as exc:  # noqa: BLE001 - degrade to "" (existing path)
        logger.warning(
            "resolver.config_export_failed", config_version_id=vid, error=str(exc)
        )
        return ""


def build_batch_resolver(
    session_factory=None,
    gitstore=None,
    evalsets_store: dict | None = None,
    suts_store: dict | None = None,
) -> Callable[[dict], tuple[Any, list[TaskRecord], str, str]]:
    """Build the production ``resolve(batch)`` injected into BatchRunner.

    Offline-safe: construction never touches the stores/DB — lookup happens
    per resolve() call, and a missing evalset raises ``NotFoundError`` (the
    trigger containment path turns that into a failed batch, not a 500).
    """

    def resolve(batch: dict) -> tuple[Any, list[TaskRecord], str, str]:
        evalset_id = str(batch.get("evalset_id", ""))
        record = (evalsets_store or {}).get(evalset_id)
        if record is None and _db_evalset_exists(session_factory, evalset_id):
            # DB metadata row exists but the task/env payload is not synced
            # to the DB in P0 — degrade honestly below.
            record = {"id": evalset_id}
        if record is None:
            raise NotFoundError(
                f"evalset {evalset_id!r} not found",
                context={"evalset_id": evalset_id},
            )

        env = _parse_env(record.get("env_yaml", ""), evalset_id)
        tasks = _parse_tasks(list(record.get("tasks") or []), evalset_id)
        params = dict(batch.get("params") or {})
        if env is None or tasks is None:
            # Honest degradation: no payload -> canned default, but tagged.
            logger.warning(
                "resolver.no_payload_fallback", evalset_id=evalset_id,
                has_env=env is not None, has_tasks=tasks is not None,
            )
            params["env_source"] = "default_offline"
            batch["params"] = params  # visible to the caller's batch record
            denv, dtasks, cfg, img = default_trigger_inputs(batch)
            # keep the marker in the returned batch-side info via params;
            # config/image still resolved from real sources below
            return denv, dtasks, cfg, _image_ref(batch, record)

        sut_id = str(batch.get("sut_id", ""))
        image_ref = _image_ref(batch, record)
        config_dir = _resolve_config_dir(
            gitstore, str(params.get("config_version_id", ""))
        )
        logger.info(
            "resolver.resolved",
            evalset_id=evalset_id,
            tasks=len(tasks),
            devices=len(env.devices),
            image_ref=image_ref,
            config_dir=bool(config_dir),
        )
        return env, tasks, config_dir, image_ref

    def _image_ref(batch: dict, record: dict) -> str:
        sut_id = str(batch.get("sut_id", ""))
        sut = (suts_store or {}).get(sut_id)
        if isinstance(sut, dict) and sut.get("image_ref"):
            return str(sut["image_ref"])
        db_ref = _db_image_ref(session_factory, sut_id)
        if db_ref:
            return db_ref
        return sut_id

    return resolve


def build_noise_run_batch_fn(
    resolver: Callable[[dict], tuple[Any, list[TaskRecord], str, str]],
    runner,
    evalset_id: str,
    sut_id: str,
) -> Callable[[list[str]], Any]:
    """N-3 bridge: run_batch_fn(task_ids) for NoiseBandCalibrator.calibrate.

    Each call resolves the evalset once (fresh env per pass), filters the
    requested tasks and runs one ``noise-calib-...`` batch. The batch-id
    prefix makes ``BatchRunner._batch_purpose`` classify it as
    ``noise_calibration`` → automatic cache bypass (ADR-17). Per-task
    metric values come from ``BatchStats.task_scores`` (D-9): ``score``
    (root_cause_accuracy, task_complete fallback) + ``task_complete``.
    """
    import itertools

    counter = itertools.count()

    async def run_batch_fn(task_ids: list[str]):
        env, all_tasks, cfg, img = resolver(
            {
                "id": f"noise-calib-{evalset_id}",
                "evalset_id": evalset_id,
                "sut_id": sut_id,
                "params": {},
            }
        )
        wanted = set(task_ids)
        tasks = [t for t in all_tasks if t.task_id in wanted]
        batch_id = f"noise-calib-{evalset_id}-{next(counter)}"
        stats = await runner.run_batch(
            batch_id,
            env,
            tasks,
            cfg,
            img,
            bypass_cache=True,
            db_meta={"evalset_id": evalset_id, "sut_id": sut_id},
        )
        return {
            str(row["task_id"]): {
                "score": float(row.get("score", 0.0)),
                "task_complete": float(row.get("task_complete", 0.0)),
            }
            for row in (getattr(stats, "task_scores", None) or [])
        }

    return run_batch_fn
