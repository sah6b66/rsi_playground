"""OpenCodeAdapter — P0 preferred SUT carrier (DD-05 §5.2, ADR-22; audit M2).

All docker/CLI interaction is deferred (asyncio.to_thread in the S11 real
backend phase); this implementation is fully offline — it only generates
config files (opencode.json) and constructs command/env records.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rsi.carrier.adapters.fake import CarrierAdapter, RunHandle, TraceChannels
from rsi.log import get_logger

if TYPE_CHECKING:  # runtime import would be circular (service imports adapters)
    from rsi.carrier.service import InstanceHandle

__all__ = ["OpenCodeAdapter"]

logger = get_logger(__name__)

IMAGE_BASE = "rsi/opencode-runtime:1.0"
IMAGE_REF = "rsi-opencode-runtime:1.0"
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"


class OpenCodeAdapter(CarrierAdapter):
    """P0 preferred carrier (ADR-22). Offline config/command generation only.

    ``real=True``（S11）时另走真实 Docker 生命周期：start_instance 创建
    sleep-infinity 容器（挂 /agent-config + /trace），collect_result 在容器
    内 exec opencode CLI 采集 stdout/exit_code，destroy_instance 回收容器。
    """

    def __init__(
        self,
        real: bool = False,
        host_pool: Any = None,
        trace_root: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        # Per-container env (cred-derived DEV_* / OPENCODE_API_KEY), keyed by
        # container_id, for the service layer to read when spawning.
        self.env_for: dict[str, dict[str, str]] = {}
        self.real = real
        self.host_pool = host_pool
        self.trace_root = Path(trace_root) if trace_root else None
        self.extra_env = dict(extra_env or {})
        # run_id -> (container_id, host)
        self._containers: dict[str, tuple[str, str]] = {}
        # instance_id -> InstanceHandle.env（collect 时叠加进 exec env）
        self._instance_env: dict[str, dict[str, str]] = {}
        self._last_env: dict[str, str] = {}

    def build_image(self, sut_id: str, config_dir: Any) -> str:
        logger.debug("opencode.build_image", sut_id=sut_id, image=IMAGE_REF)
        return IMAGE_REF

    def _read_model_config(self, config_dir: Any) -> tuple[str, str]:
        model_json = Path(config_dir) / "model.json"
        if model_json.exists():
            try:
                data = json.loads(model_json.read_text(encoding="utf-8"))
                return data.get("provider", DEFAULT_PROVIDER), data.get(
                    "model", DEFAULT_MODEL
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("opencode.model_json_unreadable", error=str(exc))
        return DEFAULT_PROVIDER, DEFAULT_MODEL

    def start(
        self, image_ref: str, config_dir: Any, permission: str, cred: Any
    ) -> str:
        """Write opencode.json into the mounted config dir; record env; offline."""
        provider, model = self._read_model_config(config_dir)
        # 工程配置 schema：模型选择必须是 "<provider>/<model>"（顶层
        # "provider" 字符串键在 opencode 1.19.5 校验失败——C2 实测）。
        opencode_config = {
            "model": f"{provider}/{model}",
            "plugins": {
                "opencode-prompt-logger": {
                    "file": "/trace/wiretap/session.ndjson",
                    "rotateBytes": False,
                    "http": True,
                }
            },
        }
        out = Path(config_dir) / "opencode.json"
        out.write_text(json.dumps(opencode_config, indent=2), encoding="utf-8")
        container_id = f"oc-{uuid.uuid4()}"
        from rsi.carrier.service import build_dev_env

        self.env_for[container_id] = build_dev_env(cred)
        self._last_env = dict(self.env_for[container_id])
        logger.debug(
            "opencode.start",
            container_id=container_id,
            image_ref=image_ref,
            permission=permission,
            env_keys=sorted(self.env_for[container_id]),
        )
        return container_id

    def submit_task(self, instance: "InstanceHandle", task: Any) -> RunHandle:
        prompt = task if isinstance(task, str) else task.prompt
        # opencode 1.19.5 实测（C2）：--format json 是事件流（FINAL_JSON 不可
        # 直接解析）；--session 仅限续接已有 ses_* 会话；--auto 非交互放行
        # 工具调用；--title 以 run_id 命名会话便于归属。
        cmd = [
            "opencode",
            "run",
            "--format",
            "default",
            "--print-logs",
            "--auto",
            "--title",
            instance.run_id,
            prompt,
        ]
        env: dict[str, str] = {
            # Per-run wiretap file so trace attribution matches
            # trace_channels (M2a); consumed by the S11 real executor.
            "OPENCODE_WIRETAP_FILE": f"/trace/wiretap/{instance.run_id}.ndjson"
        }
        container_id = getattr(instance, "container_id", None) or instance.instance_id
        env.update(self.env_for.get(container_id, {}))
        self._instance_env[instance.instance_id] = dict(
            getattr(instance, "env", {}) or {}
        )
        handle = RunHandle(
            run_id=instance.run_id,
            instance_id=instance.instance_id,
            cmd=cmd,
            env=env,
        )
        logger.debug("opencode.submit_task", run_id=instance.run_id)
        return handle

    def collect_result(self, handle: RunHandle, timeout_s: float) -> str:
        if handle.stdout is not None:
            return handle.stdout
        if not self.real:
            logger.warning(
                "opencode.collect_result.offline_stub",
                run_id=handle.run_id,
                timeout_s=timeout_s,
            )
            return ""
        client, cid = self._resolve_run(handle.run_id)
        if client is None or cid is None:
            logger.warning(
                "opencode.collect_result.no_container", run_id=handle.run_id
            )
            return ""
        container = client.containers.get(cid)
        env = dict(handle.env)
        env.update(self._instance_env.get(handle.instance_id, {}))
        # exec 继承容器 create 时的 environment（DEV_*/LLM 网关），此处仅
        # 追加 per-run wiretap 等；demux 分离 stdout/stderr，阻塞至退出。
        exit_code, out = container.exec_run(
            handle.cmd, environment=env, workdir="/workspace", demux=True
        )
        stdout_bytes = out[0] if isinstance(out, tuple) else out
        stderr_bytes = out[1] if isinstance(out, tuple) and len(out) > 1 else b""
        handle.stdout = (stdout_bytes or b"").decode("utf-8", "replace")
        handle.exit_code = int(exit_code or 0)
        if handle.exit_code != 0:
            raw = (stderr_bytes or b"").decode("utf-8", "replace")
            logger.warning(
                "opencode.exec_failed",
                run_id=handle.run_id,
                exit_code=handle.exit_code,
                stderr_head=raw[:400],
                stderr_tail=raw[-800:],
            )
        logger.info(
            "opencode.exec_done",
            run_id=handle.run_id,
            exit_code=handle.exit_code,
            stdout_len=len(handle.stdout),
        )
        self._pull_trace(container, handle.run_id)
        return handle.stdout

    def _pull_trace(self, container: Any, run_id: str) -> None:
        """远端模式：exec 后回收容器 trace（wiretap + opencode 会话库）.

        F1/A1：opencode 1.19.5 无 prompt-logger 插件，agent 行为证据在
        ``~/.local/share/opencode/opencode.db``——回收并转换为 native
        TraceEvent 行写入 ``wiretap/<run_id>.ndjson``，走既有 tail 摄取。
        """
        if not (self.trace_root and self._remote_run(run_id)):
            return
        target = self.trace_root / run_id
        target.mkdir(parents=True, exist_ok=True)
        import io
        import tarfile

        try:
            stream, _stat = container.get_archive("/trace/wiretap")
            data = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                # 成员已带 wiretap/ 前缀——解到 target 即 target/wiretap/*
                tf.extractall(target, filter="data")
            logger.debug("opencode.trace_pulled", run_id=run_id, target=str(target))
        except Exception as exc:  # noqa: BLE001 - trace 回收失败不阻断结果
            logger.warning(
                "opencode.trace_pull_failed", run_id=run_id, error=str(exc)
            )
        # 会话存档 → 事件行（A1 证据链）
        try:
            from rsi.carrier.opencode_archive import (
                OPENCODE_DB_PATH,
                session_archive_to_event_lines,
            )

            stream, _stat = container.get_archive(OPENCODE_DB_PATH)
            db_bytes = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(db_bytes)) as tf:
                member = next(
                    (m for m in tf.getmembers() if m.name.endswith("opencode.db")),
                    None,
                )
                payload = (
                    tf.extractfile(member).read() if member is not None else b""
                )
            lines = session_archive_to_event_lines(payload, run_id)
            if lines:
                wt = target / "wiretap" / f"{run_id}.ndjson"
                wt.parent.mkdir(parents=True, exist_ok=True)
                with wt.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                logger.info(
                    "opencode.session_archive_events",
                    run_id=run_id,
                    lines=len(lines),
                )
        except Exception as exc:  # noqa: BLE001 - 存档转换失败不阻断结果
            logger.warning(
                "opencode.session_archive_failed", run_id=run_id, error=str(exc)
            )

    @staticmethod
    def _tar_dir(src: Path) -> bytes:
        """目录内容打包为 tar（成员为相对路径，供 put_archive 解到目标下）。

        必须用 GNU 格式且整数 mtime：默认 PAX 格式会为高精度 mtime 生成
        ``./@PaxHeader`` 成员，docker daemon 的 archive 端点将其判为路径
        穿越并 500（E2 实测）。
        """
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
            for p in sorted(Path(src).rglob("*")):
                if p.is_file():
                    info = tf.gettarinfo(str(p), arcname=p.relative_to(src).as_posix())
                    info.mtime = int(info.mtime)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with open(p, "rb") as fh:
                        tf.addfile(info, fh)
        return buf.getvalue()

    def _remote_host(self, host: str) -> bool:
        """docker 主机是否远端（非 local URL → bind mount 不可用）。"""
        try:
            url = str(self.host_pool.config(host).docker)
        except Exception:  # noqa: BLE001 - 无 config 视为本地
            return False
        return url != "local"

    def _remote_run(self, run_id: str) -> bool:
        entry = self._containers.get(run_id)
        if entry is None:
            return False
        _, host = entry
        return self._remote_host(host)

    # -- S11 real container lifecycle -------------------------------------

    def start_instance(
        self,
        image_ref: str,
        config_dir: Any,
        permission: str,
        cred: Any,
        host: str,
        run_id: str,
    ) -> str | None:
        """Create + start the real SUT container; None = 保持占位 id."""
        if not self.real or self.host_pool is None:
            return None
        try:
            client = self.host_pool.client(host)
        except Exception as exc:  # noqa: BLE001 - host 未配置等
            logger.warning("opencode.start_instance.no_client", host=host, error=str(exc))
            return None
        if client is None:
            logger.warning("opencode.start_instance.offline_pool", host=host)
            return None
        # 复用 start()：写 opencode.json + 记录 DEV_* env（_last_env）
        self.start(image_ref, config_dir, permission, cred)
        trace_dir = self.trace_root / run_id if self.trace_root else None
        if trace_dir is not None:
            (trace_dir / "wiretap").mkdir(parents=True, exist_ok=True)
        env: dict[str, str] = {"RSI_RUN_ID": run_id}
        env.update(self._last_env)
        env.update(self.extra_env)
        remote = self._remote_host(host)
        if remote:
            # 远端 dockerd（SUT VM）：bind mount 的 Source 是本机路径，跨主机
            # 不可用——改为 put_archive 注入 config，trace 经 get_archive 回收。
            command: list[str] = [
                "sh", "-c", "mkdir -p /trace/wiretap /workspace && exec sleep infinity",
            ]
            kw: dict[str, Any] = {
                "image": image_ref,
                "command": command,
                "detach": True,
                "environment": env,
                "name": f"rsi-{run_id}",
                "labels": {"rsi.run_id": run_id},
            }
        else:
            def _bind(src: Path, target: str) -> dict[str, Any]:
                return {
                    "Type": "bind",
                    "Source": str(src.resolve()),
                    "Target": target,
                    "ReadOnly": False,
                }

            mounts = [_bind(Path(config_dir), "/agent-config")]
            if trace_dir is not None:
                mounts.append(_bind(trace_dir, "/trace"))
            kw = {
                "image": image_ref,
                "command": ["sleep", "infinity"],
                "detach": True,
                "environment": env,
                "mounts": mounts,
                "name": f"rsi-{run_id}",
                "labels": {"rsi.run_id": run_id},
            }
        container = client.containers.create(**kw)
        container.start()
        if remote:
            # docker-py put_archive(path, data)——path 在前（E2 实测参数序）
            container.put_archive("/agent-config", self._tar_dir(Path(config_dir)))
        self._containers[run_id] = (container.id, host)
        logger.info(
            "opencode.container_started",
            run_id=run_id,
            container=container.id,
            image=image_ref,
            host=host,
            remote=remote,
        )
        return container.id

    def destroy_instance(self, instance: Any) -> None:
        if not self.real:
            return
        run_id = getattr(instance, "run_id", "")
        entry = self._containers.pop(run_id, None)
        if entry is None:
            return
        cid, host = entry
        try:
            client = self.host_pool.client(host)
            container = client.containers.get(cid)
            container.stop(timeout=10)
            container.remove()
            logger.info("opencode.container_removed", run_id=run_id, container=cid)
        except Exception as exc:  # noqa: BLE001 - 清理尽力而为
            logger.warning("opencode.destroy_failed", container=cid, error=str(exc))

    def _resolve_run(self, run_id: str) -> tuple[Any, str | None]:
        entry = self._containers.get(run_id)
        if entry is None:
            return None, None
        cid, host = entry
        try:
            return self.host_pool.client(host), cid
        except Exception:  # noqa: BLE001
            return None, cid

    def trace_channels(self, instance: "InstanceHandle") -> TraceChannels:
        # real 模式下 wiretap 文件的宿主可见位置是 trace_root/<run_id>/wiretap/
        # （本地=bind mount 同一文件；远端=collect 后回收到该路径）。离线
        # 默认（无 trace_root）保持容器路径语义。
        if self.trace_root is not None:
            host = self.trace_root / instance.run_id / "wiretap"
            return TraceChannels(
                kind="ndjson",
                paths=[
                    str(host / f"{instance.run_id}.ndjson"),
                    str(host / "session.ndjson"),
                    str(host / "events.ndjson"),
                ],
            )
        return TraceChannels(
            kind="ndjson",
            paths=[
                f"/trace/wiretap/{instance.run_id}.ndjson",
                "/trace/events.ndjson",
            ],
        )
