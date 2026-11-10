"""Docker image endpoints (DD-14 §5.7, T6)：按执行主机的镜像管理。

images 数据经 services ``host_pool`` 的各主机 docker client（to_thread）；
无 client / daemon 不可达的主机列入 ``offline_hosts``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.core.errors import (
    ConflictError,
    NotFoundError,
    UpstreamError,
)
from rsi.security.auth import Claims

router = APIRouter(prefix="/docker", tags=["images"])


class DockerImageOut(BaseModel):
    host: str
    ref: str
    size_mb: float | None = None
    created_at: str | None = None
    used_by: list[str] = []


class DockerImagePage(BaseModel):
    items: list[DockerImageOut]
    offline_hosts: list[str] = []


class ImageLoadBody(BaseModel):
    host: str = Field(json_schema_extra={"example": "main"})
    tar_path: str = Field(json_schema_extra={"example": "/home/images/ipops-agent.tar"})


def _used_by(suts: dict, ref: str) -> list[str]:
    return [sid for sid, s in suts.items() if s.get("image_ref") == ref]


def _pool(request: Request):
    return request.app.state.services.get("host_pool")


@router.get("/images", response_model=DockerImagePage)
async def list_images(
    request: Request,
    user: Claims = Depends(require_perms("image:manage")),
    suts: dict = Depends(get_store("suts")),
) -> DockerImagePage:
    """各执行主机镜像列表（docker-py 经 to_thread）；离线主机入 offline_hosts。

    host_pool 未装配（P0 离线）→ items 空且 offline_hosts 为 settings
    carrier.hosts 的全部键（无 settings 服务时空列表）。
    """
    pool = _pool(request)
    items: list[DockerImageOut] = []
    offline: list[str] = []
    if pool is None:
        settings = request.app.state.services.get("settings")
        if settings is not None:
            offline = list(settings.carrier.hosts)
        return DockerImagePage(items=items, offline_hosts=offline)
    for status in pool.statuses():
        try:
            client = pool.client(status.host)
        except Exception:  # noqa: BLE001 - per-host isolation
            offline.append(status.host)
            continue
        if client is None:
            offline.append(status.host)
            continue
        try:
            images = await asyncio.to_thread(client.images.list)
        except Exception:  # noqa: BLE001 - per-host isolation
            offline.append(status.host)
            continue
        for img in images:
            tags = list(getattr(img, "tags", None) or [])
            ref = tags[0] if tags else str(getattr(img, "id", "")).replace(
                "sha256:", ""
            )[:12]
            attrs: dict = getattr(img, "attrs", None) or {}
            size = attrs.get("Size")
            items.append(
                DockerImageOut(
                    host=status.host,
                    ref=ref,
                    size_mb=round(float(size) / 1048576, 1)
                    if isinstance(size, (int, float))
                    else None,
                    created_at=attrs.get("Created"),
                    used_by=_used_by(suts, ref),
                )
            )
    return DockerImagePage(items=items, offline_hosts=offline)


@router.post("/images:load", status_code=202)
@audit.audited("image.load", object_type="image")
async def load_image(
    body: ImageLoadBody,
    request: Request,
    user: Claims = Depends(require_perms("image:manage")),
) -> dict:
    """宿主机 tar 路径导入镜像 → 202 {status, host}。

    注意：docker-py 的 ``images.load`` 在 **平台运行侧** 读取 tar 文件
    并上传到目标 daemon——``tar_path`` 必须是平台运行主机上的本地路径
    （远端执行主机场景下 tar 也要先放到平台主机上，不能是远端主机路径）。
    """
    pool = _pool(request)
    if pool is None:
        raise UpstreamError("host_pool not registered (offline assembly)")
    try:
        cfg = pool.config(body.host)
    except Exception as exc:  # noqa: BLE001 - CarrierError from pool
        raise NotFoundError(
            f"host {body.host!r} not found", context={"host": body.host}
        ) from exc
    # local/主主机场景可先做本地存在性校验；远端由 daemon 侧报错
    if getattr(cfg, "docker", "local") == "local" and not Path(body.tar_path).is_file():
        raise NotFoundError(
            "tar file not found on host",
            context={"tar_path": body.tar_path, "host": body.host},
        )
    try:
        client = pool.client(body.host)
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            f"docker client unavailable for host {body.host!r}: {exc}",
            context={"host": body.host},
        ) from exc
    if client is None:
        raise UpstreamError(
            f"docker client unavailable for host {body.host!r} (offline)",
            context={"host": body.host},
        )

    def _load() -> Any:
        with open(body.tar_path, "rb") as f:  # noqa: SIM115 - short-lived read
            return client.images.load(f)

    try:
        await asyncio.to_thread(_load)
    except FileNotFoundError:
        raise NotFoundError(
            "tar file not found on host",
            context={"tar_path": body.tar_path, "host": body.host},
        ) from None
    except Exception as exc:  # noqa: BLE001 - daemon-side failure
        raise UpstreamError(
            f"docker load failed: {exc}", context={"host": body.host}
        ) from exc
    return {"status": "loaded", "host": body.host}


@router.delete("/images/{ref:path}", status_code=204)
@audit.audited("image.delete", object_type="image")
async def delete_image(
    ref: str,
    request: Request,
    host: str = "main",
    user: Claims = Depends(require_perms("image:manage")),
    suts: dict = Depends(get_store("suts")),
) -> Response:
    """删除镜像；被 SUT 引用（suts.image_ref）时 409 列出引用。"""
    used_by = _used_by(suts, ref)
    if used_by:
        raise ConflictError(
            f"image {ref!r} is used by suts {used_by}",
            context={"ref": ref, "used_by": used_by},
        )
    pool = _pool(request)
    if pool is None:
        raise UpstreamError("host_pool not registered (offline assembly)")
    try:
        client = pool.client(host)
    except Exception as exc:  # noqa: BLE001
        raise NotFoundError(
            f"host {host!r} not found", context={"host": host}
        ) from exc
    if client is None:
        raise UpstreamError(
            f"docker client unavailable for host {host!r} (offline)",
            context={"host": host},
        )
    try:
        await asyncio.to_thread(client.images.remove, ref)
    except Exception as exc:  # noqa: BLE001
        if "No such image" in str(exc) or type(exc).__name__ == "ImageNotFound":
            raise NotFoundError(
                f"image {ref!r} not found on host {host!r}",
                context={"ref": ref, "host": host},
            ) from exc
        raise UpstreamError(
            f"docker remove failed: {exc}", context={"host": host, "ref": ref}
        ) from exc
    return Response(status_code=204)
