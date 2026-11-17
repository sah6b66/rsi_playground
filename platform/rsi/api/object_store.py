"""对象文件库（F3/B1）：内存 store 记录的重启持久化.

生产重启后 suts/evalsets/iterations 等 store 全清空（仅 users 水合），
每次重启需重跑 provision——S11 实测阻塞项。此模块以
``<RSI_HOME>/storage/object-store/<collection>/<id>.json`` 一对象一文件
落盘（写失败不阻断主流程），bootstrap 时水合回灌（内存已有同名 id 不覆盖）。

关系表同步：suts/evalsets 每次 persist/delete 同步 upsert/remove DB 存在性
行；hypotheses（B2-T5 修复）persist 同步 upsert——闭环 FR6.2/FR6.3：
bootstrap._opt_history_lookup 读 hypotheses 表，无 writer 则生产归因
"已试过 X 未果"输入恒空。payload 不入 DB，失败 warning 降级不阻断，
保证 batches/runs 外键引用与跨表 join 完整。iterations 表
CHECK(mode IN p1,p2) 与 p0 冲突，不映射——文件库仍是统一载体。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rsi.log import get_logger

__all__ = [
    "ObjectFileStore",
    "delete_record",
    "hydrate_object_stores",
    "persist_record",
]

logger = get_logger(__name__)

#: 持久化的 store 集合（重启即失且运营必需的对象）
PERSISTED_COLLECTIONS = ("suts", "evalsets", "iterations", "profiles",
                         "hypotheses")


class ObjectFileStore:
    """One-JSON-per-record file store under ``<root>/<collection>/<id>.json``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, collection: str, obj_id: str) -> Path:
        return self._root / collection / f"{obj_id}.json"

    def save(self, collection: str, record: dict[str, Any]) -> None:
        obj_id = str(record.get("id", "")).strip()
        if not obj_id:
            return
        try:
            path = self._path(collection, obj_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning(
                "object_store.save_failed",
                collection=collection,
                id=obj_id,
                error=str(exc)[:120],
            )

    def delete(self, collection: str, obj_id: str) -> None:
        try:
            self._path(collection, obj_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "object_store.delete_failed",
                collection=collection,
                id=obj_id,
                error=str(exc)[:120],
            )

    def load_all(self, collection: str) -> list[dict[str, Any]]:
        d = self._root / collection
        if not d.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(d.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    out.append(obj)
            except (OSError, json.JSONDecodeError):
                logger.warning("object_store.skip_corrupt", file=str(f))
        return out


def hydrate_object_stores(stores: dict[str, Any], root: Path | str) -> None:
    """Bootstrap 水合：文件库记录回灌内存 store（同 id 不覆盖内存）。"""
    fs = ObjectFileStore(root)
    for collection in PERSISTED_COLLECTIONS:
        target = stores.get(collection)
        if not isinstance(target, dict):
            continue
        for record in fs.load_all(collection):
            key = str(record.get("id", ""))
            if key and key not in target:
                target[key] = record
    logger.info(
        "object_store.hydrated",
        counts={c: len(stores.get(c, {})) for c in PERSISTED_COLLECTIONS},
    )


def _resolve_store(services: dict[str, Any] | None) -> ObjectFileStore | None:
    """优先取 services 内已装配的 store，否则按 settings.home 定位根目录。"""
    st = (services or {}).get("object_store")
    if isinstance(st, ObjectFileStore):
        return st
    settings = (services or {}).get("settings")
    root = getattr(settings, "home", None)
    if root is None:
        return None
    return ObjectFileStore(Path(root) / "storage" / "object-store")


_CARRIER_WHITELIST = ("claude_code", "opencode", "hermes", "custom")


def _normalize_carrier(value: Any) -> str:
    """suts 表 CHECK 约束白名单收敛——法外值落 custom，避免 IntegrityError 断链。"""
    return value if value in _CARRIER_WHITELIST else "custom"


#: ck_hypotheses_status 约束集（tables.py Hypothesis.__table_args__）
_HYP_DB_STATUSES = ("draft", "proposed", "critic_passed", "applied",
                    "accepted", "rejected", "withdrawn")
#: API store 状态 -> DB 约束状态（人工门②提交态 submitted 语义即 proposed）
_HYP_STATUS_MAP = {"submitted": "proposed"}


def _normalize_hyp_status(value: Any) -> str:
    """hypotheses 表 ck_hypotheses_status 白名单收敛——法外值落 draft。

    门控端点写入的 API store 状态是 ``submitted``（hypotheses 路由
    _VALID_STATUS 域），不在 DB CHECK 集内；直写必 IntegrityError ->
    warning 断链。与 _normalize_carrier 同一约定。
    """
    if value in _HYP_DB_STATUSES:
        return str(value)
    return _HYP_STATUS_MAP.get(str(value) if value else "", "draft")


def _hyp_payload_json(record: dict[str, Any]) -> str:
    """hypothesis_json 列构造：draft schema 载荷 + 合并顶层 target。

    必须合并顶层 ``target``——_opt_history_lookup 读 payload 的
    target/target_path 字段，而 HypothesisDraftOut 九字段无 target，
    不合并则回读 target 恒 None。draft 非法（非 dict）时降级仅 target。
    """
    draft = record.get("hypothesis_json")
    payload = dict(draft) if isinstance(draft, dict) else {}
    payload["target"] = record.get("target")
    return json.dumps(payload, ensure_ascii=False)


def _upsert_db_row(services: dict[str, Any] | None, collection: str,
                   record: dict[str, Any]) -> None:
    """Best-effort DB 存在性行同步（失败 warning 不阻断——项目 DB 写入铁律）."""
    if collection not in ("suts", "evalsets", "hypotheses"):
        return
    sf = (services or {}).get("session_factory")
    if sf is None:
        return
    try:
        from sqlmodel import select

        if collection == "suts":
            from rsi.db.tables import Sut as _T
        elif collection == "evalsets":
            from rsi.db.tables import EvalSet as _T
        else:
            from rsi.db.tables import Hypothesis as _T
        with sf() as session:
            row = session.exec(
                select(_T).where(_T.id == record["id"])
            ).first()
            if row is None:
                if collection == "suts":
                    row = _T(
                        id=record["id"],
                        name=record.get("name", record["id"]),
                        carrier=_normalize_carrier(record.get("carrier", "opencode")),
                        image_ref=record.get("image_ref", ""),
                        description=record.get("description", ""),
                        created_at=record.get("created_at", ""),
                    )
                elif collection == "evalsets":
                    row = _T(
                        id=record["id"],
                        name=record.get("name", record["id"]),
                        purpose=record.get("purpose", "iterate"),
                        created_at=record.get("created_at", ""),
                    )
                else:
                    row = _T(
                        id=record["id"],
                        sut_id=record["sut_id"],
                        iteration_id=record.get("iteration_id"),
                        hypothesis_json=_hyp_payload_json(record),
                        status=_normalize_hyp_status(
                            record.get("status", "submitted")
                        ),
                        created_at=record.get("created_at", ""),
                    )
                session.add(row)
            elif collection == "hypotheses":
                row.hypothesis_json = _hyp_payload_json(record)
                row.status = _normalize_hyp_status(
                    record.get("status", row.status)
                )
            else:
                row.name = record.get("name", row.name)
                if collection == "suts":
                    row.carrier = _normalize_carrier(record.get("carrier", row.carrier))
                    row.image_ref = record.get("image_ref", row.image_ref)
                    row.description = record.get("description", row.description)
                else:
                    row.purpose = record.get("purpose", row.purpose)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "object_store.db_upsert_failed",
            collection=collection,
            id=record.get("id"),
            error=str(exc)[:120],
        )


def _remove_db_row(services: dict[str, Any] | None, collection: str,
                   obj_id: str) -> None:
    if collection not in ("suts", "evalsets"):
        return
    sf = (services or {}).get("session_factory")
    if sf is None:
        return
    try:
        from sqlmodel import select

        if collection == "suts":
            from rsi.db.tables import Sut as _T
        else:
            from rsi.db.tables import EvalSet as _T
        with sf() as session:
            row = session.exec(select(_T).where(_T.id == obj_id)).first()
            if row is not None:
                session.delete(row)
                session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "object_store.db_delete_failed",
            collection=collection,
            id=obj_id,
            error=str(exc)[:120],
        )


def persist_record(
    services: dict[str, Any] | None, collection: str, record: dict[str, Any]
) -> None:
    """路由侧便捷写：JSON 落盘 + suts/evalsets/hypotheses 同步 upsert DB 行。"""
    store = _resolve_store(services)
    if store is None:
        return
    store.save(collection, record)
    _upsert_db_row(services, collection, record)


def delete_record(
    services: dict[str, Any] | None, collection: str, obj_id: str
) -> None:
    """JSON 删盘 + suts/evalsets 同步移除 DB 行。"""
    store = _resolve_store(services)
    if store is None:
        return
    store.delete(collection, obj_id)
    _remove_db_row(services, collection, obj_id)
