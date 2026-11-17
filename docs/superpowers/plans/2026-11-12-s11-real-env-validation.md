# S11 真实环境端到端验证 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 真实环境（RSI_HOME=E:\rsi_test + Docker Desktop + eNSP Pro + bigmodel GLM）部署 RSI Playground，补齐 S11 真实后端三缺口（LLM provider / netmiko SSH / OpenCode 真实容器执行），创建 OpenCode SUT 镜像 + 5 任务评测集，跑通 CLI 模式 RSI 会话全流程。

**Architecture:** 代码侧按平台既有扩展点最小侵入接入：LLMClient 注入真实 handler（Anthropic 兼容 /v1/messages）、SshPool 注入 netmiko 工厂、OpenCodeAdapter 增加 real 模式（docker create/exec/rm，挂 /agent-config + /trace）。全部由 settings 开关控制，默认关闭——离线测试基线不变。部署侧照 INSTALL.md/install.sh 执行并记录偏差，对象创建走 API（curl）。

**Tech Stack:** Python 3.12 + uv + FastAPI + SQLModel/SQLite；netmiko（新增依赖）；docker SDK（已有依赖）；httpx（已有）；OpenCode（容器内 CLI Agent，npm `opencode-ai`）。

**Spec:** 本对话任务书（用户提供：eNSP Pro 192.168.56.102:8443、6 设备 192.168.96.11-24、SSH 22/CLI_Agen/Adm@1234、LLM token/base_url/glm-5.1、RSI_HOME=E:\rsi_test）+ docs/_rsi_playground_design.md（DD-02/04/05/12）+ docs/_rsi_playground_configuration.md。

## Global Constraints

- 全量回归 `uv run pytest -q` 零失败（当前基线 1350+ passed），所有真实后端默认关闭（offline 安全）
- 实现类任务串行执行（CLAUDE.md 规程），每批：测试先行 → 回归 → 独立 commit
- 分支 `feat/s11-real-env-fixes` 自 main 切出；完成后 merge --no-ff + push origin main（已授权）
- DB 写入 try/except + warning 不阻断主流程；SandboxService 池化会话必须经 `_run_sync`/`asyncio.to_thread`（H1：DeviceSession.lock 绑定事件循环）
- 模块名/配置键约定：`ensp_pro.py` 下划线 vs 配置枚举 `ensp-pro` 连字符——勿"纠正"
- 密钥：RSI_JWT_SECRET = `openssl rand -hex 32`，RSI_MASTER_KEY = `openssl rand -base64 32`，落 E:\rsi_test\rsi-env.sh（不提交）
- LLM token 仅入 settings.yaml（内网明文策略）与本地镜像，不提交仓库
- `docs/superpowers/plans/`、`platform/.audit*/`、报告文件是工作产物，合并前不提交
- eNSP 事实：WEB https://192.168.56.102:8443（自签证书 verify_tls=false）；设备 Spine-01=192.168.96.11, Spine-02=.12, Leaf-01=.21 … Leaf-04=.24；SSH 端口 22 用户 CLI_Agen 密码 Adm@1234；设备类型 vrp（华为 VRP，netmiko device_type=huawei）
- SUT LLM 配置（用户指定）：ANTHROPIC_AUTH_TOKEN=a0f8113582874ad1a0d631a38f00705c.6Ksdkf3HBhPNUGZy，base https://open.bigmodel.cn/api/anthropic，model glm-5.1

---

## 关键事实（执行者需要知道的机制，全部已核实）

1. **装配链**：`bootstrap.build_services`（platform/rsi/bootstrap.py:165）组装一切。LLM handler 固定 `_offline_llm_handler`（:88，judge 只回 mock JSON）；`ssh_pool = SshPool()`（:274，默认 MockDeviceSession）；adapters dict `{"opencode": OpenCodeAdapter(), "fake": FakeCarrier()}`（:257）。
2. **批处理管线**：`BatchRunner.run_task`（orchestrator/batch.py:753）9 步：prepare（sandbox.restore_task 回滚 + exec_cli 注入故障）→ create_instance（carrier）→ submit_task → collect → trace ingest → judge → restore_task → destroy。`adapter.start()` 在整个批处理链路**从未被调用**（grep 证实）——真实容器启动必须由本次实现接入。
3. **config 目录链**：`POST /batches` 走 resolver（orchestrator/resolver.py:169）；`config_version_id` 来自 batch params → GitStore.export 到临时目录 → instrumentor overlay → create_instance 挂载。SUT 文件上传 `PUT /suts/{id}/files/{path}`（白名单 `skills/.*`、`model.json`、`system-prompt.md` 等，optimizer/targets.py:28）→ `POST /suts/{id}/versions:snapshot` 出 version_id。
4. **凭据注入**：env.yaml `ssh_access` → `CredentialResolver.resolve_env` → `DEV_<NAME>_{HOST,PORT,USER,PASS}`（连字符→下划线，`DEV_SPINE_01_PASS`，security/credentials.py:151-189）→ `build_dev_env`（carrier/service.py:58）进容器 env。read-write 权限时加 `DEV_RW=1`。
5. **输出契约**：SUT stdout 末尾 `<FINAL_JSON>{...}</FINAL_JSON>` → `extract_task_result`（carrier/extract.py）映射 task_complete/root_cause/actions/confidence。exit 137/139 = instance_crashed；exit 0 + 空 stdout = parse_error。
6. **回滚**：每任务 prepare 前 `display current-configuration` 备份到 labs_dir/backups/<task_id>，任务后 replay（L1）；prebuilt 模式 snapshot/restore 是 no-op，reset = 厂商 reset_cmd。
7. **会话**：`POST /iterations {sut_id, evalset_id, prep_mode:"cli", mode:"p0"|"p1", label, profile?}`（api/routers/iterations.py:48-179）；IterationEngine 直接调 run_batch（不走 resolver）。单批评测 `POST /batches {evalset_id, sut_id, purpose, params:{config_version_id}}`。
8. **发现的真实 bug**：resolver._parse_tasks（resolver.py:99-113）要求行键 `id`/`prompt`，而 `:import`（evalsets.py:412-417）原样存 tasks.jsonl 行=TaskRecord 形状（`task_id`/`task.prompt`）——导入评测集跑批必然 ValidationError。Task A1 修复。
9. **OpenCodeAdapter 现状**（carrier/adapters/opencode.py）：start() 写 opencode.json 进 config_dir + 记录 env_for[container_id]，返回假 id；submit_task 构造 `["opencode","run","--format","json","--print-logs","--session",run_id,prompt]` + env（OPENCODE_WIRETAP_FILE + DEV_*）；collect_result 是 offline stub 返回 ""。RunHandle 有 stdout/exit_code 字段留给真实执行器。
10. **HostService**（carrier/service.py）：create_instance 注册 InstanceHandle（container_id=instance_id 占位）+ NetPolicy（纯内存记账，无真实 docker network 操作）+ host_pool.increment；destroy 不触容器。HostPool 已有真实 docker SDK 分支（hosts.py:62-81，local=npipe/from_env，ssh://…=DockerClient）。
11. **settings 结构**（settings.py）：`llm.providers.anthropic.{api_key,base_url}`、`llm.defaults.judge.{model,temperature}`、`llm.cassette_mode(replay|record|off)`、`sandbox.backends.<key>.{kind,url,verify_tls,subnets,devices}`、`carrier.hosts.<key>.{docker,subnets,max_instances}`、`carrier.adapters.<key>.image_base`、`carrier.task_timeout_s`。settings.yaml 放 `<RSI_HOME>/settings.yaml`。
12. **部署脚本**：deploy/install.sh（git-bash 可跑：uv sync --extra dev → 复制 settings.yaml.example → `RSI_HOME=var python -m rsi.init` → 可选前端构建）；deploy/check-subnets.ps1 校验 backends+hosts 子网唯一。启动 `cd platform && uv run uvicorn rsi.api.app:create_app --factory --host 0.0.0.0 --port 8000`，健康 `GET /api/v1/system/health`（无鉴权）、自检 `GET /api/v1/system/doctor`。

---

# Phase A — 平台代码：S11 真实后端（分支 feat/s11-real-env-fixes）

### Task A1: 修复 resolver 解析 TaskRecord 形状的评测集行

**Files:**
- Modify: `platform/rsi/orchestrator/resolver.py:88-119`（_parse_tasks）
- Test: `platform/tests/unit/orchestrator/test_resolver_task_shapes.py`（新建）

**Interfaces:**
- Produces: `_parse_tasks` 同时接受 ①TaskRecord 形状（`task_id`,`title`,`prepare`,`task{prompt,context,tool_constraints}`,`expected`,`metrics`）②平铺形状（`id`,`prompt`）；Task A1 之后的 D/E 阶段导入评测集依赖此行为。

- [ ] **Step 1: 写失败测试**

```python
"""resolver._parse_tasks 双形状解析（A1：导入评测集跑批链路修复）."""
from rsi.orchestrator.resolver import _parse_tasks

TASK_RECORD_ROW = {
    "task_id": "fault/leaf-uplink-down-01",
    "title": "上联口误关",
    "prepare": [{"action": "exec_cli", "entries": [{"device": "Leaf-01", "cli_lines": ["interface GE1/0/2", "shutdown"]}]}],
    "task": {"prompt": "定位故障。以 <FINAL_JSON>{...}</FINAL_JSON> 输出。"},
    "expected": {"root_cause": {"expected": "上联口被 shutdown"}},
    "metrics": ["root_cause_accuracy"],
}
FLAT_ROW = {"id": "inspection/x-01", "prompt": "巡检", "expected": {}}


def test_taskrecord_shape_parsed():
    records = _parse_tasks([TASK_RECORD_ROW], "es-1")
    assert records[0].task_id == "fault/leaf-uplink-down-01"
    assert "FINAL_JSON" in records[0].task.prompt
    assert records[0].prepare[0].entries[0].device == "Leaf-01"
    assert records[0].expected.root_cause.expected == "上联口被 shutdown"
    assert records[0].metrics == ["root_cause_accuracy"]


def test_flat_shape_still_parsed():
    records = _parse_tasks([FLAT_ROW], "es-1")
    assert records[0].task_id == "inspection/x-01"
    assert records[0].task.prompt == "巡检"


def test_bad_row_raises_with_line():
    import pytest
    from rsi.core.errors import ValidationError
    with pytest.raises(ValidationError) as ei:
        _parse_tasks([{"no_id": 1}], "es-1")
    assert "line 1" in str(ei.value)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/orchestrator/test_resolver_task_shapes.py -q`
Expected: FAIL（KeyError "id" 被 ValidationError 包装路径外抛或直接 KeyError——现状必挂）

- [ ] **Step 3: 实现**——_parse_tasks 行解析改为：

```python
        for i, t in enumerate(raw_tasks):
            try:
                task_id = str(t.get("task_id") or t.get("id") or "")
                if not task_id:
                    raise KeyError("task_id/id")
                raw_task = t.get("task")
                if isinstance(raw_task, dict):
                    spec = TaskSpec.model_validate(raw_task)
                else:
                    spec = TaskSpec(prompt=str(t.get("prompt", "")))
                records.append(
                    TaskRecord(
                        task_id=task_id,
                        title=str(t.get("title") or task_id),
                        prepare=list(t.get("prepare") or []),
                        task=spec,
                        expected=(
                            ExpectedAnswer.model_validate(t["expected"])
                            if t.get("expected") is not None
                            else ExpectedAnswer()
                        ),
                        metrics=list(t.get("metrics") or []),
                    )
                )
```
（metrics 空列表合法：TaskRecord.metrics 默认 []）

- [ ] **Step 4: 回归** `uv run pytest tests/unit/orchestrator tests/integration -q` → PASS
- [ ] **Step 5: Commit** `fix(orchestrator): resolver 解析 TaskRecord 形状评测集行（:import 跑批链路）`

### Task A2: LLM 真实 provider（Anthropic 兼容）

**Files:**
- Create: `platform/rsi/llm/providers.py`
- Modify: `platform/rsi/bootstrap.py:209-226`（LLM 装配段）
- Test: `platform/tests/unit/llm/test_providers.py`（新建）

**Interfaces:**
- Produces: `AnthropicCompatHandler(api_key, base_url, model, timeout_s=120.0, max_tokens=4096)`，可调用签名 `(component, prompt_name, vars, *, temperature=0.0) -> str`（即 llm/client.py 的 Handler 协议）。bootstrap 条件：`cassette_mode=="off"` 且 `providers.anthropic.api_key` 非空时启用，model 取 `defaults.judge.model or "glm-5.1"`。

- [ ] **Step 1: 写失败测试**（monkeypatch httpx.post）

```python
"""AnthropicCompatHandler（A2）：请求形状/响应拼接/错误路径."""
from unittest.mock import MagicMock

import pytest

from rsi.llm.providers import AnthropicCompatHandler


def _ok_response():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "content": [{"type": "text", "text": "部分一"}, {"type": "text", "text": "部分二"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return resp


def test_call_posts_messages_api(monkeypatch):
    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return _ok_response()
    monkeypatch.setattr("rsi.llm.providers.httpx.post", fake_post)
    h = AnthropicCompatHandler(api_key="tk", base_url="https://open.bigmodel.cn/api/anthropic", model="glm-5.1")
    out = h("judge", "root_cause", {"prompt": "判卷"}, temperature=0.0)
    assert out == "部分一部分二"
    assert captured["url"] == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    body = captured["kw"]["json"]
    assert body["model"] == "glm-5.1" and body["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "判卷"}]
    assert captured["kw"]["headers"]["x-api-key"] == "tk"
    assert captured["kw"]["headers"]["Authorization"] == "Bearer tk"
    assert captured["kw"]["headers"]["anthropic-version"] == "2023-06-01"


def test_vars_without_prompt_falls_back_to_json(monkeypatch):
    captured = {}
    monkeypatch.setattr("rsi.llm.providers.httpx.post", lambda url, **kw: (captured.update(kw), _ok_response())[1])
    AnthropicCompatHandler(api_key="tk")("judge", "root_cause", {"a": "b"})
    assert '"a"' in captured["json"]["messages"][0]["content"]


def test_http_error_raises(monkeypatch):
    resp = MagicMock(status_code=401, text="unauthorized")
    monkeypatch.setattr("rsi.llm.providers.httpx.post", lambda url, **kw: resp)
    with pytest.raises(RuntimeError, match="401"):
        AnthropicCompatHandler(api_key="tk")("judge", "root_cause", {"prompt": "x"})


def test_empty_text_raises(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"content": []}
    monkeypatch.setattr("rsi.llm.providers.httpx.post", lambda url, **kw: resp)
    with pytest.raises(RuntimeError, match="no text"):
        AnthropicCompatHandler(api_key="tk")("judge", "root_cause", {"prompt": "x"})
```

- [ ] **Step 2: 确认失败**（ModuleNotFoundError）
- [ ] **Step 3: 实现** `platform/rsi/llm/providers.py`：

```python
"""Anthropic 兼容真实 provider handler（S11，DD-02 §5.1 真实接线）.

经 LLMClient 注入的 Handler 协议调用 Anthropic 兼容 ``/v1/messages``
（官方 API 或 bigmodel 等兼容网关）。鉴权同时发 x-api-key 与
Authorization: Bearer——兼容网关两种取法都接受。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rsi.log import get_logger

__all__ = ["AnthropicCompatHandler"]

logger = get_logger(__name__)


class AnthropicCompatHandler:
    """Callable LLM handler posting to an Anthropic-compatible /v1/messages."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "glm-5.1",
        timeout_s: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    def __call__(
        self,
        component: str,
        prompt_name: str,
        vars: dict[str, str],
        *,
        temperature: float = 0.0,
    ) -> str:
        prompt = str(vars.get("prompt") or json.dumps(vars, ensure_ascii=False))
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        resp = httpx.post(
            f"{self.base_url}/v1/messages",
            json=body,
            headers=headers,
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"llm provider http {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        if not text:
            raise RuntimeError("llm provider returned no text blocks")
        usage = data.get("usage") or {}
        logger.debug(
            "llm.provider_call",
            component=component,
            prompt=prompt_name,
            model=self.model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        return text
```

- [ ] **Step 4: bootstrap 接线**（bootstrap.py LLM 段替换 handler 选择）：

```python
    # -- LLM（cassette off + 配了 anthropic key → 真实 handler；否则离线 mock）--
    from rsi.llm.providers import AnthropicCompatHandler

    judge_cfg = settings.llm.defaults.get("judge")
    _anth = settings.llm.providers.get("anthropic")
    handler: Callable[..., str] = _offline_llm_handler
    if (
        settings.llm.cassette_mode == "off"
        and _anth is not None
        and _anth.api_key
    ):
        handler = AnthropicCompatHandler(
            api_key=_anth.api_key,
            base_url=_anth.base_url,
            model=(judge_cfg.model if judge_cfg and judge_cfg.model else "glm-5.1"),
        )
        logger.info("bootstrap.llm_real_provider", base_url=_anth.base_url or "default")
    llm_client = LLMClient(
        handler=handler,
        ...  # 其余参数不变
    )
```

- [ ] **Step 5: 回归** `uv run pytest tests/unit/llm tests/unit -q`（默认 settings 下走 offline 分支，现有 bootstrap 测试不受影响）
- [ ] **Step 6: Commit** `feat(llm): Anthropic 兼容真实 provider handler（cassette off 时接线）`

### Task A3: netmiko 真实 SSH 设备会话

**Files:**
- Create: `platform/rsi/sandbox/netmiko_session.py`
- Modify: `platform/rsi/sandbox/sshpool.py:156-189`（factory 签名加 access）
- Modify: `platform/rsi/settings.py:366-384`（SandboxSettings 增 ssh_backend）
- Modify: `platform/rsi/bootstrap.py:274`（按 ssh_backend 注入工厂）
- Modify: `platform/pyproject.toml`（+netmiko）
- Test: `platform/tests/unit/sandbox/test_netmiko_session.py`（新建）；grep `session_factory=` 更新受影响用例

**Interfaces:**
- Produces: `NetmikoDeviceSession(key, vendor_profile, access: SshAccess)`（DeviceSession 子类，_send 经 `asyncio.to_thread` 走 netmiko `send_command`，`_close` 走 disconnect，`is_alive` 真探）；模块级 `netmiko_session_factory(key, profile, access)`；SshPool 构造参数 `session_factory` 新签名 `(key, profile, access) -> DeviceSession`。

- [ ] **Step 1: pyproject 加依赖**：`cd platform && uv add netmiko`（产 lock 变更）
- [ ] **Step 2: 写失败测试**（sys.modules 注入 fake netmiko，验证 to_thread 调用/lock 语义/is_alive/close）

```python
"""NetmikoDeviceSession（A3）：真实 SSH 会话离线单测."""
import asyncio
import sys
import types

from rsi.core.models import SshAccess
from rsi.sandbox.netmiko_session import NetmikoDeviceSession, netmiko_session_factory
from rsi.sandbox.sshpool import SessionKey
from rsi.sandbox.vendor import VENDOR_MAP


class _FakeConn:
    def __init__(self, *a, **kw):
        self.kw = kw
        self.sent: list[str] = []
        self.alive = True

    def send_command(self, cli, **kw):
        self.sent.append(cli)
        return f"OUT<{cli}>"

    def is_alive(self):
        return self.alive

    def disconnect(self):
        self.alive = False


def _patch_netmiko(monkeypatch, cls=_FakeConn):
    mod = types.ModuleType("netmiko")
    mod.ConnectHandler = cls
    monkeypatch.setitem(sys.modules, "netmiko", mod)


def test_send_via_netmiko(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(ip="192.168.96.11", port=22, username="CLI_Agen", password="Adm@1234")
    s = netmiko_session_factory(
        SessionKey(ip=access.ip, port=access.port, lab_id="lab-x"),
        VENDOR_MAP["vrp"], access,
    )
    out = asyncio.run(s.exec_cli("display version"))
    assert out.ok and out.output == "OUT<display version>"


def test_connect_uses_access_credentials(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(ip="192.168.96.21", port=2222, username="u", password="p")
    s = NetmikoDeviceSession(SessionKey(access.ip, 2222, "lab"), VENDOR_MAP["vrp"], access)
    asyncio.run(s.exec_cli("display clock"))
    assert s._conn.kw["host"] == "192.168.96.21"
    assert s._conn.kw["port"] == 2222
    assert s._conn.kw["username"] == "u"
    assert s._conn.kw["device_type"] == "huawei"


def test_close_and_liveness(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(ip="1.2.3.4", username="u", password="p")
    s = NetmikoDeviceSession(SessionKey("1.2.3.4", 22, "l"), VENDOR_MAP["vrp"], access)
    assert s.is_alive() is False  # 未连接
    asyncio.run(s.exec_cli("display version"))
    assert s.is_alive() is True
    asyncio.run(s.close())
    assert s._conn.alive is False


def test_sshpool_factory_receives_access(monkeypatch):
    _patch_netmiko(monkeypatch)
    from rsi.sandbox.sshpool import SshPool

    pool = SshPool(session_factory=netmiko_session_factory)
    access = SshAccess(ip="192.168.96.12", username="u", password="p")
    session = asyncio.run(pool.device(SessionKey(access.ip, 22, "lab-1"), access, "vrp"))
    assert isinstance(session, NetmikoDeviceSession)
```

- [ ] **Step 3: 确认失败**（模块不存在）
- [ ] **Step 4: 实现** `netmiko_session.py`：

```python
"""netmiko 真实设备会话（S11，DD-04 §4.2 真实 SSH 切片）.

延迟连接、命令经 ``asyncio.to_thread`` 执行（H1：DeviceSession.lock
绑定创建它的循环，所有阻塞 IO 必须离开事件循环）。
"""

from __future__ import annotations

import asyncio

from rsi.core.models import SshAccess
from rsi.sandbox.sshpool import DeviceSession, SessionKey
from rsi.sandbox.vendor import VendorProfile

__all__ = ["NetmikoDeviceSession", "netmiko_session_factory"]


class NetmikoDeviceSession(DeviceSession):
    def __init__(
        self, key: SessionKey, vendor: VendorProfile, access: SshAccess
    ) -> None:
        super().__init__(key, vendor)
        self._access = access
        self._conn = None

    def _connect(self):
        if self._conn is not None and self._conn.is_alive():
            return self._conn
        from netmiko import ConnectHandler  # 延迟导入：离线环境不必装

        self._conn = ConnectHandler(
            device_type=self.vendor.netmiko_device_type,
            host=self._access.ip,
            port=self._access.port,
            username=self._access.username,
            password=self._access.password or "",
            conn_timeout=15,
            banner_timeout=30,
        )
        return self._conn

    def _send_sync(self, cli: str, read_timeout_s: int) -> str:
        conn = self._connect()
        return conn.send_command(cli, read_timeout=read_timeout_s)

    async def _send(self, cli: str, read_timeout_s: int) -> str:
        return await asyncio.to_thread(self._send_sync, cli, read_timeout_s)

    def is_alive(self) -> bool:
        try:
            return bool(self._conn is not None and self._conn.is_alive())
        except Exception:  # noqa: BLE001 - 传输层异常按死链处理
            return False

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.disconnect)


def netmiko_session_factory(key, profile, access):
    return NetmikoDeviceSession(key, profile, access)
```

- [ ] **Step 5: SshPool factory 签名**——sshpool.py `device()` 内改为：

```python
        session = (
            self._factory(key, profile, access)
            if self._factory is not None
            else MockDeviceSession(key, profile)
        )
```
（docstring 同步更新；grep `session_factory=` 全仓，更新测试注入工厂为三参签名——MockDeviceSession 构造兼容多余参数则包一层 lambda）

- [ ] **Step 6: settings 增键**——SandboxSettings：

```python
    ssh_backend: Literal["mock", "netmiko"] = "mock"  # S11: 设备会话真实 SSH 开关
```

- [ ] **Step 7: bootstrap 接线**（替换 `ssh_pool = SshPool()` 一行）：

```python
    if settings.sandbox.ssh_backend == "netmiko":
        from rsi.sandbox.netmiko_session import netmiko_session_factory

        ssh_pool = SshPool(session_factory=netmiko_session_factory)
        logger.info("bootstrap.ssh_netmiko_pool")
    else:
        ssh_pool = SshPool()  # default factory = MockDeviceSession (offline)
```

- [ ] **Step 8: 回归** `uv run pytest -q` 全量零失败
- [ ] **Step 9: Commit** `feat(sandbox): netmiko 真实设备会话 + SshPool 工厂签名携带 ssh_access`

### Task A4: OpenCodeAdapter 真实 Docker 执行

**Files:**
- Modify: `platform/rsi/carrier/adapters/fake.py`（CarrierAdapter 增两个默认 no-op：`start_instance`/`destroy_instance`）
- Modify: `platform/rsi/carrier/adapters/opencode.py`（real 模式：容器创建/exec 采集/销毁）
- Modify: `platform/rsi/carrier/service.py`（create_instance 调 start_instance、destroy 调 destroy_instance）
- Modify: `platform/rsi/settings.py:390-418`（CarrierAdapterSettings 增 real/env；CarrierSettings 增 trace_dir）
- Modify: `platform/rsi/bootstrap.py:257-266`（按 settings 构造 real adapter）
- Test: `platform/tests/unit/carrier/test_opencode_real.py`（新建）

**Interfaces:**
- CarrierAdapter 新默认方法（不破坏 FakeCarrier/既有测试）：

```python
    def start_instance(
        self, image_ref: str, config_dir: Any, permission: str, cred: Any,
        host: str, run_id: str,
    ) -> str | None:
        """S11 真实容器启动；返回 container_id，None=无真实容器（沿用占位 id）。"""
        return None

    def destroy_instance(self, instance: "InstanceHandle") -> None:
        """S11 真实容器回收；默认 no-op。"""
        return None
```
- OpenCodeAdapter 构造：`OpenCodeAdapter(real=False, host_pool=None, trace_root=None, extra_env=None)`；real=True 时 start/collect/destroy 走 docker SDK。
- HostService.create_instance：overlay 之后 `container_id = adapter.start_instance(...) or instance_id`；HostService.destroy：先 `adapter.destroy_instance(instance)`（try/except）。

- [ ] **Step 1: 写失败测试**（FakeDockerClient 桩验证挂载/env/exec/exit_code/销毁）

```python
"""OpenCodeAdapter real 模式（A4）：docker 生命周期离线单测."""
from pathlib import Path

from rsi.carrier.adapters.opencode import OpenCodeAdapter
from rsi.carrier.service import InstanceHandle


class FakeContainers:
    def __init__(self, client):
        self.client = client

    def create(self, image, **kw):
        c = FakeContainer(self.client, image, kw)
        self.client.created.append(c)
        return c

    def get(self, cid):
        for c in self.client.created:
            if c.id == cid:
                return c
        raise KeyError(cid)


class FakeContainer:
    _seq = 0

    def __init__(self, client, image, kw):
        FakeContainer._seq += 1
        self.client = client
        self.id = f"cid{FakeContainer._seq:04d}"
        self.image = image
        self.kw = kw
        self.started = removed = False
        self.execs: list[dict] = []

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def remove(self):
        self.removed = True

    def exec_run(self, cmd, environment=None, workdir=None, demux=False):
        self.execs.append({"cmd": cmd, "env": environment or {}})
        stdout = b'<FINAL_JSON>{"task_complete": true}</FINAL_JSON>'
        return (0, (stdout, b"")) if demux else (0, stdout)


class FakeDockerClient:
    def __init__(self):
        self.created: list[FakeContainer] = []
        self.containers = FakeContainers(self)


class FakeHostPool:
    def __init__(self, client):
        self._c = client

    def client(self, host):
        return self._c


def _adapter(tmp_path):
    return OpenCodeAdapter(
        real=True,
        host_pool=FakeHostPool(FakeDockerClient()),
        trace_root=tmp_path / "trace",
        extra_env={"ANTHROPIC_AUTH_TOKEN": "tk"},
    )


def _instance(adapter, cid):
    return InstanceHandle(
        instance_id="inst-1", sut_id="img", run_id="run-1", host="main",
        container_id=cid, config_dir=Path("."), carrier="opencode",
    )


def test_real_start_creates_container(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_instance("rsi/opencode-netops:1.0", tmp_path, "read-only",
                           {"devices": {"Spine-01": {"user": "u", "pass": "p", "host": "h", "port": 22}}},
                           "main", "run-1")
    (c,) = adapter.host_pool._c.created
    assert c.started and c.kw["image"] == "rsi/opencode-netops:1.0"
    mounts = {m["Target"]: m["Source"] for m in c.kw["mounts"]}
    assert mounts["/agent-config"] == str(tmp_path)
    assert mounts["/trace"].endswith("trace/run-1")
    assert c.kw["environment"]["DEV_SPINE_01_USER"] == "u"
    assert c.kw["environment"]["ANTHROPIC_AUTH_TOKEN"] == "tk"


def test_real_collect_runs_exec_and_fills_handle(tmp_path):
    adapter = _adapter(tmp_path)
    cid = adapter.start_instance("img", tmp_path, "read-write", None, "main", "run-1")
    inst = _instance(adapter, cid)
    handle = adapter.submit_task(inst, "检查设备。以 <FINAL_JSON>{...}</FINAL_JSON> 输出。")
    out = adapter.collect_result(handle, 30)
    assert out.endswith("</FINAL_JSON>")
    assert handle.exit_code == 0 and handle.stdout == out
    (c,) = adapter.host_pool._c.created
    assert c.execs[0]["cmd"][0] == "opencode"
    assert c.execs[0]["env"]["OPENCODE_WIRETAP_FILE"] == "/trace/wiretap/run-1.ndjson"


def test_real_destroy_removes_container(tmp_path):
    adapter = _adapter(tmp_path)
    cid = adapter.start_instance("img", tmp_path, "read-only", None, "main", "run-1")
    adapter.destroy_instance(_instance(adapter, cid))
    (c,) = adapter.host_pool._c.created
    assert c.removed


def test_offline_default_unchanged():
    adapter = OpenCodeAdapter()
    assert adapter.start_instance("img", Path("."), "ro", None, "main", "r") is None
    assert adapter.destroy_instance(_instance(adapter, "x")) is None
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现基类默认方法**（fake.py CarrierAdapter，见 Interfaces 代码）
- [ ] **Step 4: 实现 OpenCodeAdapter real 模式**——opencode.py 增补（保留原 offline 路径）：

```python
class OpenCodeAdapter(CarrierAdapter):
    def __init__(self, real: bool = False, host_pool=None, trace_root=None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.env_for: dict[str, dict[str, str]] = {}
        self.real = real
        self.host_pool = host_pool
        self.trace_root = Path(trace_root) if trace_root else None
        self.extra_env = dict(extra_env or {})
        self._containers: dict[str, str] = {}  # run_id -> container_id
        self._instance_env: dict[str, dict[str, str]] = {}

    # start() 原样保留（写 opencode.json + 记录 env_for），另加真实启动：

    def start_instance(self, image_ref, config_dir, permission, cred, host, run_id):
        if not self.real or self.host_pool is None:
            return None
        client = self.host_pool.client(host)
        if client is None:
            logger.warning("opencode.start_instance.no_client", host=host)
            return None
        # 复用 start() 的 opencode.json 生成逻辑（config 已含则合并保留 provider 段）
        self.start(image_ref, config_dir, permission, cred)
        trace_dir = self.trace_root / run_id if self.trace_root else None
        if trace_dir is not None:
            (trace_dir / "wiretap").mkdir(parents=True, exist_ok=True)
        env = {"RSI_RUN_ID": run_id, **self.env_for[f"oc-{run_id}"] if False else {}}
        # 注：env_for 键由 start() 返回的假 id 索引——改为按调用序取最新一条：
        env.update(self._last_env)
        env.update(self.extra_env)
        kw: dict[str, Any] = {
            "image": image_ref,
            "command": ["sleep", "infinity"],
            "detach": True,
            "environment": env,
            "mounts": [f"{Path(config_dir).resolve()}:/agent-config"],
            "name": f"rsi-{run_id}",
        }
        if trace_dir is not None:
            kw["mounts"].append(f"{trace_dir.resolve()}:/trace")
        container = client.containers.create(**kw)
        container.start()
        self._containers[run_id] = container.id
        logger.info("opencode.container_started", run_id=run_id, container=container.id)
        return container.id
```
（实现时把 `self._last_env` 改为 `start()` 里 `self.env_for[self._last_fake_id]` 的清晰等价：start() 末尾 `self._last_env = env_dict`，env_for 索引沿用假 id，两处都写）

  submit_task 增记 `self._instance_env[instance.instance_id] = dict(instance.env)`；collect_result real 分支：

```python
    def collect_result(self, handle: RunHandle, timeout_s: float) -> str:
        if handle.stdout is not None:
            return handle.stdout
        if not self.real:
            logger.warning("opencode.collect_result.offline_stub", run_id=handle.run_id)
            return ""
        import time as _time

        client = self._client_for_run(handle.run_id)
        container = client.containers.get(self._containers[handle.run_id])
        env = dict(handle.env)
        env.update(self._instance_env.get(handle.instance_id, {}))
        import docker as _docker  # exec 走低层 API 以取流式 stdout + 退出码
        exec_id = client.api.exec_create(
            container.id, handle.cmd, environment=env, workdir="/workspace"
        )["Id"]
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        chunks: list[bytes] = []
        deadline = _time.monotonic() + timeout_s
        for out, _err in stream:
            if out:
                chunks.append(out)
            if _time.monotonic() > deadline:
                logger.warning("opencode.exec_timeout", run_id=handle.run_id)
                break
        handle.stdout = b"".join(chunks).decode("utf-8", "replace")
        handle.exit_code = int(client.api.exec_inspect(exec_id)["ExitCode"] or 0)
        logger.info("opencode.exec_done", run_id=handle.run_id, exit_code=handle.exit_code,
                    stdout_len=len(handle.stdout))
        return handle.stdout

    def destroy_instance(self, instance) -> None:
        if not self.real:
            return
        cid = self._containers.pop(getattr(instance, "run_id", ""), None)
        if cid is None:
            return
        try:
            client = self._client_for_run(getattr(instance, "run_id", ""))
            container = client.containers.get(cid)
            container.stop(timeout=10)
            container.remove()
        except Exception as exc:  # noqa: BLE001 - 清理尽力而为
            logger.warning("opencode.destroy_failed", container=cid, error=str(exc))

    def _client_for_run(self, run_id: str):
        host = "main"  # 单主机部署；多主机时由 HostService 选择并回填
        return self.host_pool.client(host)
```
（多主机注记：本阶段单机 Docker Desktop；HostService 已在 InstanceHandle.host 记录 host，后续扩展把 host 存入 _containers 值即可）

- [ ] **Step 5: HostService 接线**——create_instance 在 overlay 之后、net attach 之前：

```python
        container_id = instance_id
        start_instance = getattr(self._adapter_for(carrier_type), "start_instance", None)
        if callable(start_instance):
            real_cid = start_instance(
                image_ref, config_dir, permission, cred, selected_host, run_id
            )
            if real_cid:
                container_id = real_cid
```
（InstanceHandle 构造用 container_id 变量；cleanup 分支补 `adapter.destroy_instance`）；destroy() 开头：

```python
        try:
            self._adapter_for(instance.carrier).destroy_instance(instance)
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 6: settings 增键**：

```python
class CarrierAdapterSettings(BaseModel):
    model_config = _C
    image_base: str = ""
    real: bool = False  # S11: 真实容器执行开关
    env: dict[str, str] = Field(default_factory=dict)  # 注入容器的额外 env（LLM 网关等）

# CarrierSettings 增加：
    trace_dir: str = ""  # 空默认 <RSI_HOME>/carrier-trace
```
（Settings 派生属性处补 trace_dir 解析，仿 labs_dir 模式）

- [ ] **Step 7: bootstrap 接线**（替换 adapters dict 构造）：

```python
    oc_cfg = settings.carrier.adapters.get("opencode")
    adapters: dict[str, Any] = {
        "opencode": OpenCodeAdapter(
            real=bool(oc_cfg and oc_cfg.real),
            host_pool=host_pool,
            trace_root=Path(settings.carrier_trace_dir) if (oc_cfg and oc_cfg.real) else None,
            extra_env=(oc_cfg.env if oc_cfg else None),
        ),
        "fake": FakeCarrier(),
    }
```

- [ ] **Step 8: 回归** `uv run pytest -q` 全量零失败（默认 real=False 全走原路径）
- [ ] **Step 9: Commit** `feat(carrier): OpenCodeAdapter 真实 Docker 执行（create/exec/销毁 + 挂载注入）`

### Task A5: 全量回归 + 合并

- [ ] `cd platform && uv run pytest -q` → 0 failed
- [ ] `git checkout main && git merge feat/s11-real-env-fixes --no-ff && git push origin main`（SSL 失败等 10-15s 重试）

---

# Phase B — 部署与安装验证（RSI_HOME=E:\rsi_test）

### Task B1: 照指导书跑 install.sh（验证安装链路）

- [ ] **Step 1**：`cd E:\rsi_playground && bash deploy/install.sh`（git-bash；预期 uv sync --extra dev + pytest 全过 + settings.yaml 复制 + init 打印 admin 密码 + 前端构建若 npm 在）
- [ ] **Step 2**：记录与 INSTALL.md 的偏差（脚本断言、路径、提示文案）；有错则修 install.sh/INSTALL.md 并 commit `fix(deploy): ...`
- [ ] **Step 3**：安装链路验证结论记入 `.s11-report.md`（仓库外工作产物，见 E4）

### Task B2: 真实环境目录 + 密钥 + settings.yaml

- [ ] **Step 1**：`mkdir -p /e/rsi_test`；生成密钥并落 `E:\rsi_test\rsi-env.sh`：

```bash
JWT=$(openssl rand -hex 32); MK=$(openssl rand -base64 32)
cat > /e/rsi_test/rsi-env.sh <<EOF
export RSI_HOME="E:/rsi_test"
export RSI_JWT_SECRET="$JWT"
export RSI_MASTER_KEY="$MK"
export RSI_ADMIN_PASSWORD="RsiAdmin#2026"
EOF
```
- [ ] **Step 2**：`source /e/rsi_test/rsi-env.sh && cd platform && uv run python -m rsi.init`（E:\rsi_test 下建 platform.db + git 裸仓 + admin）
- [ ] **Step 3**：写 `E:\rsi_test\settings.yaml`（完整内容，全键）：

```yaml
server: {host: 0.0.0.0, port: 8000}
database: {url: ""}   # 空 → <RSI_HOME>/platform.db
llm:
  providers:
    anthropic:
      api_key: "a0f8113582874ad1a0d631a38f00705c.6Ksdkf3HBhPNUGZy"
      base_url: "https://open.bigmodel.cn/api/anthropic"
  defaults:
    judge: {model: "glm-5.1", temperature: 0.0}
  cassette_mode: "off"
  max_concurrent_calls: 4
carrier:
  task_timeout_s: 1800
  max_instances_per_host: 10
  trace_dir: ""      # 默认 <RSI_HOME>/carrier-trace
  adapters:
    opencode:
      image_base: "rsi/opencode-netops:1.0"
      real: true
      env:
        ANTHROPIC_AUTH_TOKEN: "a0f8113582874ad1a0d631a38f00705c.6Ksdkf3HBhPNUGZy"
        ANTHROPIC_BASE_URL: "https://open.bigmodel.cn/api/anthropic"
        ANTHROPIC_MODEL: "glm-5.1"
  hosts:
    main: {docker: "local", subnets: ["10.100.0.0/16"], max_instances: 10}
sandbox:
  ssh_backend: "netmiko"
  build_retry: 2
  ready_timeout_s: 600
  backends:
    fake: {kind: "fake"}
    ensp-pro-1:
      kind: "ensp-pro"
      url: "https://192.168.56.102:8443"
      verify_tls: false
      subnets: ["192.168.96.0/24"]
      devices:
        Spine-01: {ip: 192.168.96.11, port: 22}
        Spine-02: {ip: 192.168.96.12, port: 22}
        Leaf-01: {ip: 192.168.96.21, port: 22}
        Leaf-02: {ip: 192.168.96.22, port: 22}
        Leaf-03: {ip: 192.168.96.23, port: 22}
        Leaf-04: {ip: 192.168.96.24, port: 22}
```
- [ ] **Step 4**：`powershell -ExecutionPolicy Bypass -File deploy/check-subnets.ps1`（仓库根；确认子网无重叠告警逻辑可跑）
- [ ] **Step 5**：`uv run python -c "from rsi.settings import Settings; s=Settings(); print(s.llm.cassette_mode, s.sandbox.ssh_backend, s.carrier.adapters['opencode'].real)"` 验证三层加载（env RSI_HOME 指向 E:/rsi_test）

### Task B3: 启动服务 + 自检

- [ ] **Step 1**：`source /e/rsi_test/rsi-env.sh && cd platform && uv run uvicorn rsi.api.app:create_app --factory --host 0.0.0.0 --port 8000`（后台运行，日志 > /e/rsi_test/server.log）
- [ ] **Step 2**：`curl -s http://localhost:8000/api/v1/system/health` → ok；`curl -s -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"RsiAdmin#2026"}'` → token 存环境变量 `$TOK`
- [ ] **Step 3**：`curl -s -H "Authorization: Bearer $TOK" http://localhost:8000/api/v1/system/doctor` → 逐项核对：JWT/master 非临时、docker reachable、ensp-pro-1 URL 探活 ok（https 自签 verify_tls=false）；偏差记录
- [ ] **Step 4**：真实连通性三点验证（失败即止，按风险处置）：
  - `uv run python -c "import httpx; print(httpx.post('https://open.bigmodel.cn/api/anthropic/v1/messages', headers={'x-api-key':'<token>','anthropic-version':'2023-06-01'}, json={'model':'glm-5.1','max_tokens':32,'messages':[{'role':'user','content':'ping'}]}, timeout=60).status_code)"` → 200
  - `docker run --rm rsi/opencode-netops:1.0 ssh -o StrictHostKeyChecking=no ... Spine-01 display version`（镜像 C 阶段建成后补跑；先手工 `ssh CLI_Agen@192.168.96.11` 验证主机可达）
  - 容器→192.168.96.0/24 路由：`docker run --rm alpine nc -zv 192.168.96.11 22`；不通则启用 Docker Desktop host networking（settings.json `"hostNetworking": true`）并在 settings.yaml `carrier.adapters.opencode` 下网络模式调整（A4 实现里 containers.create 已可加 network_mode，必要时热补）

---

# Phase C — OpenCode SUT 镜像（skill + LLM 配置注入）

### Task C1: 设备端口勘察（喂给评测集与镜像文档）

- [ ] **Step 1**：对 6 台设备逐台（主机 ssh 客户端或后续容器）采集：`display version`、`display interface brief`、`display lldp neighbor brief`（若支持）、`display current-configuration | include ^#|interface|description|ip address`
- [ ] **Step 2**：落 `E:\rsi_test\survey.md`：每台设备的上联口（→Spine-01/02）、下联口（→PC）、IP 清单；推导 Leaf-01 上联 Spine-01 的接口名 `<L01-UP1>` 等实值（后续 D1/C2 引用）

### Task C2: 镜像构建文件（deploy/sut-opencode/，提交仓库）

**Files:**
- Create: `deploy/sut-opencode/Dockerfile`
- Create: `deploy/sut-opencode/entrypoint.sh`
- Create: `deploy/sut-opencode/defaults/opencode.json`
- Create: `deploy/sut-opencode/defaults/AGENTS.md`
- Create: `deploy/sut-opencode/defaults/skills/network-ops/SKILL.md`
- Create: `deploy/sut-opencode/defaults/skills/network-ops/huawei-cli.sh`

- [ ] **Step 1: Dockerfile**：

```dockerfile
# RSI SUT Agent: OpenCode + 网络运维 skill + GLM(Anthropic 兼容) LLM 配置
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-client sshpass iputils-ping procps \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g opencode-ai@latest && npm cache clean --force

# LLM 配置 + skill 注入镜像（/opt/rsi-defaults；entrypoint 在 /agent-config 缺项时补拷）
COPY defaults/ /opt/rsi-defaults/
# 平台挂载 config 版本到 /agent-config；opencode 全局配置目录指向 /rsi-config
ENV XDG_CONFIG_HOME=/rsi-config \
    ANTHROPIC_AUTH_TOKEN=a0f8113582874ad1a0d631a38f00705c.6Ksdkf3HBhPNUGZy \
    ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic \
    ANTHROPIC_MODEL=glm-5.1
RUN mkdir -p /rsi-config/opencode /workspace /agent-config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/rsi-defaults/skills/network-ops/huawei-cli.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
```

- [ ] **Step 2: entrypoint.sh**（把平台挂载的 /agent-config 桥接进 opencode 全局配置；镜像内置项做缺省兜底）：

```bash
#!/bin/sh
# /agent-config（平台 SUT config 版本挂载点）→ opencode 配置桥接：
# - opencode.json / skills / AGENTS.md 优先用挂载版，缺项回补镜像内置
set -e
SRC=/opt/rsi-defaults
AC=/agent-config
for f in opencode.json AGENTS.md model.json; do
  [ -s "$AC/$f" ] || cp "$SRC/$( [ -f "$SRC/$f" ] && echo "$f" || echo defaults/opencode.json )" "$AC/$f" 2>/dev/null || true
done
[ -d "$AC/skills" ] || cp -r "$SRC/skills" "$AC/skills"
# opencode 全局配置目录（$XDG_CONFIG_HOME/opencode）：软链保证实时反映挂载内容
CFG=/rsi-config/opencode
ln -sf "$AC/opencode.json" "$CFG/opencode.json"
ln -sfn "$AC/skills" "$CFG/skill"
mkdir -p /workspace
[ -f /workspace/AGENTS.md ] || ln -sf "$AC/AGENTS.md" /workspace/AGENTS.md
exec "$@"
```
（注：`$CFG/skill` 用 ln -sfn 整目录链；opencode skill 目录名为 `skill/`，平台白名单为 `skills/`——桥接在此对齐）

- [ ] **Step 3: defaults/opencode.json**（LLM 配置核心：自定义 provider 指向 bigmodel，token 走 env 注入优先）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "bigmodel/glm-5.1",
  "provider": {
    "bigmodel": {
      "npm": "@anthropic-ai/sdk",
      "name": "GLM (bigmodel Anthropic-compatible)",
      "options": {
        "baseURL": "https://open.bigmodel.cn/api/anthropic",
        "authToken": "a0f8113582874ad1a0d631a38f00705c.6Ksdkf3HBhPNUGZy"
      },
      "models": {
        "glm-5.1": {"name": "GLM-5.1"}
      }
    }
  },
  "plugins": {
    "opencode-prompt-logger": {
      "file": "/trace/wiretap/session.ndjson",
      "rotateBytes": false,
      "http": true
    }
  }
}
```
（运行时以 /agent-config 内平台/上传版为准；platform adapter 生成的 plugins 段与此合并语义见 A4——adapter 保留既有 provider 段只补 plugins）

- [ ] **Step 4: defaults/AGENTS.md**（SUT 行为契约，平台 `suts/{id}/system-prompt.md` 同文上传）：

```markdown
# 网络运维 Agent 工作约定

你是网络运维工程师，通过 SSH 操作华为 VRP 设备完成诊断/巡检/割接任务。

## 设备访问
设备登录信息在环境变量里：`DEV_<设备名大写下划线>_HOST/_PORT/_USER/_PASS`
（如 DEV_SPINE_01_HOST / DEV_SPINE_01_PASS，设备名中 `-` 已转 `_`）。
统一用仓库 skill 脚本执行命令：
  bash /agent-config/skills/network-ops/huawei-cli.sh Spine-01 "display version"
多行配置（system-view 内）用 -c 逐条传入。

## 输出契约（必须遵守）
最终结论必须以如下格式作为回答的最后一段输出（这是评测解析的唯一依据）：
<FINAL_JSON>{"task_complete": true|false, "root_cause": "...", "actions": ["..."], "confidence": 0.0-1.0}</FINAL_JSON>

## 安全约束
- 只读诊断优先；改配置前先 `display current-configuration` 记录现状
- 修复类任务完成后必须验证（重发检查命令确认状态）
- 不要保存配置（save）除非任务明确要求
```

- [ ] **Step 5: defaults/skills/network-ops/SKILL.md + huawei-cli.sh**：

```markdown
---
name: network-ops
description: 华为 VRP 设备 SSH 运维技能——登录凭据取自 DEV_* 环境变量，统一脚本执行 display/system-view 命令
---

# 华为设备 SSH 运维

凭据环境变量命名：`DEV_$(echo <设备名> | tr 'a-z-' 'A-Z_')_HOST/_PORT/_USER/_PASS`。

单条命令：
    bash skills/network-ops/huawei-cli.sh <设备名> "display interface brief"

进入配置视图多行（逐条 -c）：
    bash skills/network-ops/huawei-cli.sh Leaf-01 -c "interface <PORT>" -c "description To-Spine-01"

常用诊断序列：display version / display interface brief / display lldp neighbor /
display ip routing-table / display current-configuration | include <关键字>
```

```bash
#!/usr/bin/env bash
# 用法: huawei-cli.sh <设备名> "display ..." | huawei-cli.sh <设备名> -c "line1" -c "line2"
set -euo pipefail
dev="$1"; shift
norm=$(echo "$dev" | tr 'a-z-' 'A-Z_')
host_var="DEV_${norm}_HOST"; port_var="DEV_${norm}_PORT"
user_var="DEV_${norm}_USER"; pass_var="DEV_${norm}_PASS"
for v in "$host_var" "$port_var" "$user_var" "$pass_var"; do
  [ -n "${!v:-}" ] || { echo "missing env $v" >&2; exit 2; }
done
args=()
if [ "${1:-}" = "-c" ]; then
  shift
  printf '%s\n' system-view "$@" return
else
  printf '%s\n' "$@"
fi | sshpass -p "${!pass_var}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    -p "${!port_var}" "${!user_var}@${!host_var}"
```

- [ ] **Step 6: 构建与冒烟**：

```bash
cd deploy/sut-opencode && docker build -t rsi/opencode-netcode:1.0 -t rsi/opencode-netops:1.0 .
docker run --rm rsi/opencode-netops:1.0 opencode --version
docker run --rm -e DEV_SPINE_01_HOST=192.168.96.11 -e DEV_SPINE_01_PORT=22 \
  -e DEV_SPINE_01_USER=CLI_Agen -e DEV_SPINE_01_PASS='Adm@1234' \
  rsi/opencode-netops:1.0 bash /opt/rsi-defaults/skills/network-ops/huawei-cli.sh Spine-01 "display version"
docker run --rm rsi/opencode-netops:1.0 opencode run --format json --print-logs \
  "回复且仅回复 <FINAL_JSON>{\"task_complete\":true}</FINAL_JSON>"
```
（最后一条验证：glm-5.1 连通 + stdout 含 FINAL_JSON + `--format json/--print-logs` 标志被当前 opencode 版本接受；若标志不被接受，回改 opencode.py 的 cmd 列表去掉不支持项，重跑 A4 回归后 commit）
- [ ] **Step 7: Commit** `feat(deploy): OpenCode SUT 镜像构建文件（skill+GLM 配置注入）`

### Task C3: 网络连通复核（容器 → eNSP 设备网段）

- [ ] `docker run --rm rsi/opencode-netops:1.0 bash -c "nc -zv 192.168.96.11 22 && nc -zv 192.168.96.24 22"` → 通则继续；不通启用 Docker Desktop host networking 后在 A4 `containers.create` 增 `network_mode="host"`（Windows Docker Desktop 4.34+ 支持，settings.json 开启），补测试 + commit

---

# Phase D — 评测集（CLI 模式，5 任务）

### Task D1: 评测集三件套（deploy/evalsets/ensp-spineleaf/，提交仓库）

**Files:**
- Create: `deploy/evalsets/ensp-spineleaf/env.yaml`
- Create: `deploy/evalsets/ensp-spineleaf/tasks.jsonl`
- Create: `deploy/evalsets/ensp-spineleaf/configs/{Spine-01,Spine-02,Leaf-01,Leaf-02,Leaf-03,Leaf-04}.cfg`

- [ ] **Step 1: env.yaml**（端口实值来自 C1 勘察 `<L01-UP1>` 等替换进 topology；prepare 引用同源）：

```yaml
name: ensp-spineleaf
backend: ensp-pro
devices:
  - {name: Spine-01, type: vrp, version: "", config: configs/Spine-01.cfg}
  - {name: Spine-02, type: vrp, version: "", config: configs/Spine-02.cfg}
  - {name: Leaf-01, type: vrp, version: "", config: configs/Leaf-01.cfg}
  - {name: Leaf-02, type: vrp, version: "", config: configs/Leaf-02.cfg}
  - {name: Leaf-03, type: vrp, version: "", config: configs/Leaf-03.cfg}
  - {name: Leaf-04, type: vrp, version: "", config: configs/Leaf-04.cfg}
topology:
  links:
    - {endpoints: ["Leaf-01:<L01-UP1>", "Spine-01:<S01-P1>"]}
    - {endpoints: ["Leaf-01:<L01-UP2>", "Spine-02:<S02-P1>"]}
    - {endpoints: ["Leaf-02:<L02-UP1>", "Spine-01:<S01-P2>"]}
    - {endpoints: ["Leaf-02:<L02-UP2>", "Spine-02:<S02-P2>"]}
    - {endpoints: ["Leaf-03:<L03-UP1>", "Spine-01:<S01-P3>"]}
    - {endpoints: ["Leaf-03:<L03-UP2>", "Spine-02:<S02-P3>"]}
    - {endpoints: ["Leaf-04:<L04-UP1>", "Spine-01:<S01-P4>"]}
    - {endpoints: ["Leaf-04:<L04-UP2>", "Spine-02:<S02-P4>"]}
ssh_access:
  Spine-01: {ip: 192.168.96.11, port: 22, username: CLI_Agen, password: "Adm@1234"}
  Spine-02: {ip: 192.168.96.12, port: 22, username: CLI_Agen, password: "Adm@1234"}
  Leaf-01: {ip: 192.168.96.21, port: 22, username: CLI_Agen, password: "Adm@1234"}
  Leaf-02: {ip: 192.168.96.22, port: 22, username: CLI_Agen, password: "Adm@1234"}
  Leaf-03: {ip: 192.168.96.23, port: 22, username: CLI_Agen, password: "Adm@1234"}
  Leaf-04: {ip: 192.168.96.24, port: 22, username: CLI_Agen, password: "Adm@1234"}
ready_checks: []
config_apis: {}
external_deps: []
```
（configs/*.cfg：占位基线 `sysname <设备名>` 即可——prebuilt 模式不推送配置，仅满足 EnvSpec 必填）

- [ ] **Step 2: tasks.jsonl**（5 任务；`<端口>` 全部来自 C1 勘察实值。行为语义）：

1. `inspection/topology-inventory-01` 巡检采集——无 prepare；prompt 要求逐台采集 display version + display interface brief 并汇总健康结论；expected.checklist（6 台可达/接口统计正常）+ critical_steps（display version、display interface brief）+ metrics [task_complete, missed_check]
2. `fault/leaf-uplink-down-01` 上联误关——prepare exec_cli Leaf-01 `["interface <L01-UP1>", "shutdown"]`；prompt："Leaf-01 下挂的 192.168.11.0/24 网段部分主机到服务器网段出现间歇性丢包/路径切换，请定位根因"；expected.root_cause.expected="Leaf-01 上联 Spine-01 的接口 <L01-UP1> 被 shutdown，流量切换到 Spine-02 单路径"；final_state=[{device Leaf-01, cli "display interface <L01-UP1>", expect_contains ["current state: UP"], expect_absent ["Administratively DOWN"]}]；critical_steps=[display interface, display current-configuration]；metrics [root_cause_accuracy, diagnostic_steps]

   注：该任务考察"定位+修复"（read-write 权限时 Agent 修复；judge final_state 在 judge 后 restore 前实测——设计上 StateAssert 在判定时执行，restore 在 judge 之后，与 ADR-20 L1 一致）
3. `fault/leaf-hostlink-down-01` 下联误关——prepare exec_cli Leaf-03 下联 PC-SERVER-1 口 shutdown；prompt："192.168.13.31（PC-SERVER-1）失联，同网段 .32 正常，请定位"；expected.root_cause="Leaf-03 下联 PC-SERVER-1 的接口被 shutdown"；final_state 同型
4. `cutover/if-desc-standard-01` 描述规范割接——无 prepare；prompt："为 Leaf-01 上联两接口配置描述 `To_Spine-01`/`To_Spine-02`（接口：<L01-UP1>/<L01-UP2>），完成后验证"；expected.final_state=[{device Leaf-01, cli "display current-configuration interface <L01-UP1>", expect_contains ["To_Spine-01"]} ×2]；critical_steps=[display current-configuration]；metrics [task_complete]
5. `inspection/fullmesh-reachability-01` 全互联连通验证——无 prepare；prompt："验证任意 Leaf 经两条独立路径可达两台 Spine（display lldp neighbor 或路由表证据），输出结论"；expected.checklist（4 Leaf × 2 Spine 邻接/路由存在）+ critical_steps=[display lldp neighbor | display ip routing-table]；metrics [task_complete, missed_check]

（JSON 行严格按 TaskRecord 形状：task_id/title/prepare/task.prompt 内含 FINAL_JSON 约定文本/expected/metrics；对照 examples/evalsets/bgp-faults-holdout/tasks.jsonl 逐字段核对）

- [ ] **Step 3: 打包导入**：

```bash
cd deploy/evalsets/ensp-spineleaf && zip -r /tmp/ensp-spineleaf.zip env.yaml tasks.jsonl configs/
curl -s -X POST http://localhost:8000/api/v1/evalsets:import \
  -H "Authorization: Bearer $TOK" -F "file=@/tmp/ensp-spineleaf.zip" -F "name=ensp-spineleaf"
```
- [ ] **Step 4: 验证**：`curl -s -H "Authorization: Bearer $TOK" http://localhost:8000/api/v1/evalsets` → 5 任务、env_yaml 完整；若 422 按 errors 修 jsonl
- [ ] **Step 5: Commit** `feat(evalset): eNSP Pro spine-leaf 真实环境评测集（5 任务）`

---

# Phase E — SUT Agent 创建 + RSI 会话（CLI 模式）

### Task E1: SUT 对象 + 配置版本

- [ ] **Step 1**：`curl -X POST /api/v1/suts -d '{"name":"opencode-netops","carrier":"opencode","image_ref":"rsi/opencode-netops:1.0","description":"OpenCode+GLM 网络运维 Agent（真实容器）"}'` → sut_id
- [ ] **Step 2**：上传文件（`PUT /api/v1/suts/{id}/files/<path>`，body {"content":..., "commit":false}）：
  - `model.json`：`{"provider": "bigmodel", "model": "glm-5.1", "temperature": 0.2}`（白名单 model_config）
  - `system-prompt.md`：C2 AGENTS.md 全文（白名单 system_prompt）
  - `skills/network-ops/SKILL.md`、`skills/network-ops/huawei-cli.sh`：C2 同源文件（白名单 skills）
  - `opencode.json` 不在白名单（TARGET_PATHS 无此项）——LLM 配置经镜像内置 + 容器 env 注入双通道，已覆盖
- [ ] **Step 3**：`POST /api/v1/suts/{id}/versions:snapshot {"message":"baseline v1"}` → version_id；promote 到生产链（`POST /suts/{id}/versions:promote`，按路由实际字段）；`POST /api/v1/suts/{id}/verify-image` 确认镜像在 Docker 中存在

### Task E2: 单批评测冒烟（真实管线验证）

- [ ] **Step 1**：`curl -X POST /api/v1/batches -d '{"evalset_id":"<es-id>","sut_id":"<sut-id>","purpose":"smoke","params":{"config_version_id":"<version_id>"}}'`
- [ ] **Step 2**：轮询 `GET /api/v1/batches/{id}` 至 completed/failed；`GET /api/v1/batches/{id}/runs` + `GET /api/v1/runs/{id}` 看 result_summary；`GET /api/v1/runs/{id}/judgment` 看 det/llm/verdict 三层——确认 LLM 裁判走的是真实 glm-5.1（llm_calls 表有真实 token 计数：`sqlite3 E:/rsi_test/platform.db "select model, input_tokens, output_tokens from llm_calls limit 5"`）
- [ ] **Step 3**：排查路径：server.log 搜 `opencode.container_started/exec_done`、`llm.provider_call`、prepare 的 exec_cli 回滚日志；`docker ps -a | grep rsi-run`；`E:/rsi_test/carrier-trace/<run_id>/wiretap/`
- [ ] **Step 4**：任何管线缺陷按 CLAUDE.md 规程开修复批（测试先行→回归→commit）

### Task E3: RSI 会话（iteration，CLI 模式）

- [ ] **Step 1**：先查 profile 契约：`grep -n "class OptimizationProfile\|stop\|budget" platform/rsi/api/routers/profiles.py platform/rsi/db/tables.py | head -30`，构造最小合法 profile（goals/budget），或用已有默认 profile_id
- [ ] **Step 2**：`curl -X POST /api/v1/iterations -d '{"sut_id":"<sut-id>","evalset_id":"<es-id>","mode":"p0","prep_mode":"cli","label":"真实环境验证-CLI","profile":{...}}'` → iteration_id
- [ ] **Step 3**：驱动一轮：`GET /api/v1/iterations/{id}` 看状态机（首轮 baseline batch 跑完 → awaiting gate）→ 按 current_step 依次 `POST /api/v1/iterations/{id}/gates`（confirm-direction / hypothesis / review / decision 各 gate 的 GateBody 字段按 iterations.py 路由核对）→ 直到 round 1 完成
- [ ] **Step 4**：结果核验：`GET /api/v1/dashboard/metrics`、`GET /api/v1/runs?iteration=<id>`、judgments 汇总；确认：5 任务 verdict 落库、prepare 故障注入+回滚真实发生（设备侧 `display current-configuration interface <L01-UP1>` 无 shutdown 残留）、容器全部销毁（`docker ps -a | grep rsi-` 为空）

### Task E4: 收尾

- [ ] **Step 1**：清理工作产物（不提交）：`docs/superpowers/plans/` 本计划、`.s11-report.md` 留在仓库外（E:\rsi_test）
- [ ] **Step 2**：汇总报告（对话内输出）：安装指导书/脚本验证结论（偏差+修复 commit 列表）、S11 三缺口实现清单、5 任务会话结果（verdict 统计）、遗留问题
- [ ] **Step 3**：合并推送：`git checkout main && git merge feat/s11-real-env-fixes --no-ff && git push origin main`

---

## Self-Review 结论

- 覆盖检查：用户任务书 ①配置生成+依赖部署（Phase B）②OpenCode 镜像+skill+LLM 注入+SUT（Phase C/E1）③5 任务评测集（Phase D）④CLI 会话验证（Phase E2/E3）——全覆盖；附带验证安装指导书（B1）与修复真实缺口（A1）
- 占位符扫描：C1 端口勘察是运行时数据采集步骤（无法预知实值），`<L01-UP1>` 类标记是采集产物替换点，非计划缺口；其余步骤均含完整命令/代码
- 类型一致性：Handler 签名、CarrierAdapter 新默认方法、SshPool 工厂三参签名在各 Task 间一致
- 风险清单：opencode CLI 标志兼容性（C2 Step 6 验证+回退）、容器→eNSP 路由（C3 验证+host-network 回退）、iteration profile 契约（E3 Step 1 先查后构造）、instrumentor smoke 在真实模式每 config 版本多 2 次容器探针（成本可接受，不改）
