"""对象文件库（F3/B1）：内存 store 记录的重启持久化.

生产重启后 suts/evalsets/iterations 等 store 全清空（仅 users 水合），
每次重启需重跑 provision——S11 实测阻塞项。此模块以
``<RSI_HOME>/storage/object-store/<collection>/<id>.json`` 一对象一文件
落盘（写失败不阻断主流程），bootstrap 时水合回灌（内存已有同名 id 不覆盖）。

关系表（suts/evalsets/iterations DDL）保持原用途（报表/审计），不在此
强映射——iterations 表 CHECK(mode IN p1,p2) 与 p0 冲突、evalsets 表无
载荷列，文件库是统一且无迁移负担的载体。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rsi.log import get_logger

__all__ = ["ObjectFileStore", "hydrate_object_stores", "persist_record"]

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


def persist_record(
    services: dict[str, Any] | None, collection: str, record: dict[str, Any]
) -> None:
    """路由侧便捷写：从 services 取 settings 定位根目录后落盘。"""
    settings = (services or {}).get("settings")
    root = getattr(settings, "home", None)
    if root is None:
        return
    ObjectFileStore(Path(root) / "storage" / "object-store").save(
        collection, record
    )


def delete_record(
    services: dict[str, Any] | None, collection: str, obj_id: str
) -> None:
    settings = (services or {}).get("settings")
    root = getattr(settings, "home", None)
    if root is None:
        return
    ObjectFileStore(Path(root) / "storage" / "object-store").delete(
        collection, obj_id
    )
