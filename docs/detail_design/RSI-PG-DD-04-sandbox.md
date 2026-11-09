# RSI-PG-DD-04 sandbox —— 仿真环境抽象层

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-04 |
| 版本 | v1.2（ADR-25 预建模式：平台 URL 探活 + PrebuiltBackend + 设备级 SSH 寻址；ADR-24 多实例并行保留） |
| 上游 | 设计 3.4、接口 §4；FR3/FR4、ADR-20/24/25；ADR-12/13 |
| 交付切片 | S3（Backend 抽象+Fake+readiness）；S11（真实后端） |

## 1. 追溯与范围

**职责**：SandboxService 七操作门面（构建/快照/恢复/重置/销毁/评测准备 prepare/任务后恢复 restore_task）+ 设备级 SSH 会话池（netmiko，键=(ip,port)）+ 平台 URL 探活（ADR-25）+ 就绪探测 + 后端适配器（Prebuilt（ensp-pro/pnetlab 共用）/Fake）。**恢复 ≠ 重建**（L1 任务级按手段恢复 / L2 环境级兜底，ADR-20）。

**不做什么**：不含故障编译层（v2.8 已删——任务直接 exec_cli）；不管理 docker（carrier 职责）；OTLP/LLM 无关。

## 2. 文件与类清单

```
rsi/sandbox/__init__.py
rsi/sandbox/service.py        # SandboxService + ConfigApiCaller + OpsJournal
rsi/sandbox/sshpool.py        # SshPool / DeviceSession / HostSession
rsi/sandbox/vendor.py         # VENDOR_MAP（device_type/错误模式/恢复模板 SSOT）
rsi/sandbox/readiness.py      # ReadinessChecker
rsi/sandbox/platform_probe.py    # PlatformProbe（平台 URL 探活，TTL 缓存，ADR-25）
rsi/sandbox/backends/__init__.py
rsi/sandbox/backends/base.py      # SandboxBackend ABC（含非抽象 status()）
rsi/sandbox/backends/prebuilt.py  # PrebuiltBackend（ensp-pro/pnetlab 共用，预建模式 ADR-25）
rsi/sandbox/backends/fake.py      # FakeSandboxBackend（内存虚拟设备）
```

## 3. 数据结构

（LabHandle/AppliedAction/SnapRef 见 DD-01 §3.8）

```python
class LlmSettings: ...  # 占位引用（见配置文档）

READONLY_PREFIXES: tuple[str, ...] = ("display", "show", "ping", "tracert",
                                      "screen-length", "disp")

class ExecResult(BaseModel):
    ok: bool; output: str; matched_errors: list[str] = []; duration_ms: int

class VendorProfile(BaseModel):           # vendor.py SSOT（借鉴 nl2cli VendorProfile，P0 收敛两厂商）
    netmiko_device_type: str              # vrp→"huawei"; ce→"huawei"; ios→"cisco_ios"
    error_patterns: tuple[str, ...]       # 逐行锚定正则（见 §5.2）
    reset_cmd: tuple[str, ...]            # vrp: ("reset saved-configuration",) 前置；ios: ("write erase",)
    enter_config: str                     # vrp: "system-view"; ios: "configure terminal"
    exit_config: str                      # "return" / "end"
    save_config: tuple[str, ...]          # vrp: ("save","y")

VENDOR_MAP: dict[str, VendorProfile] = {"vrp": …, "ce": …, "ios": …}

class SessionKey(NamedTuple):             # v1.1（ADR-24）
    ip: str; port: int                    # 池键=(IP,端口)——全局唯一（不同沙箱同名设备端点不同）
    lab_id: str                           # 归属追溯（不参与唯一性；lab 销毁时按此批量回收会话）
```

## 4. 对外接口

### 4.1 SandboxService（接口 §4 落地）

```python
class SandboxService:
    def __init__(self, backends: dict[str, SandboxBackend], settings: SandboxSettings,
                 trace_log=…): ...
    async def build_lab(self, spec: EnvSpec, backend_key: str | None = None) -> LabHandle
        # backend_key 缺省=LabScheduler 选实例（ADR-24：同类型多实例中最少活跃 lab；显式指定优先；
        #   ADR-25：过滤 BackendStatus.available=False 候选，全不可用→EnvError）
        # backend.build(spec)（预建语义，ADR-25）→ **端点解析**：env.ssh_access 基准（权威，ADR-24 ②升为基准）
        #   + 站点覆盖先行套用 → PrebuiltBackend 透传 endpoints=dict(env.ssh_access)
        #   → 写 labs.device_endpoints_json + LabHandle.endpoints → wait_ready → 写 labs 行 + ops.jsonl 头
    async def wait_ready(self, lab: LabHandle, timeout_s: int | None = None) -> ReadyReport
        # 预建语义（ADR-25）：ReadinessChecker.check，缺省每设备 ssh_reachable（SSH 端口可达=就绪）
    async def snapshot(self, lab, name: str) -> SnapRef           # native→backend；否则 degraded 标记
    async def restore(self, lab, snap: SnapRef) -> None           # L2：native 回滚 / degraded=reset+重放 ops.jsonl 前缀
    async def reset(self, lab) -> None
    async def destroy(self, lab) -> None
        # 预建语义（ADR-25）：不销毁平台侧任何对象——仅 pop 内部登记 + ssh_pool.close_lab(lab_id) 回收设备会话
    async def prepare(self, lab, task_id: str, actions: list[PrepareAction]) -> list[AppliedAction]
        # 逐动作：exec_cli→先备份再下发（§5.3）；call_api→ConfigApiCaller；写 ops.jsonl；失败抛 EnvError（调用方重试）
    async def restore_task(self, lab, applied: list[AppliedAction]) -> None
        # L1：exec_cli→按备份恢复（§5.4）；call_api→undo 重调；后接 ready 快检；失败重试≤2→EnvError（触发 L2）
    async def exec_readonly(self, lab, device: str, cli: str) -> str
        # 白名单前缀校验（小写化）→ DeviceSession.exec_cli；越界→DeviceError（TU-035）
    async def backend_status(self) -> list[BackendStatus]         # GET /sandbox/backends 数据源
    async def health_check(self, lab: LabHandle, timeout_s: int = 5) -> bool
        """F3 v1.1 任务前轻量探测：选 lab.endpoints 首个设备发 'display clock'（白名单内）
        → SSH 成功且输出含时间戳→True；超时/SSH 失败→False
        BatchRunner 每任务前调用，连续 2 次 False→判定沙箱离线→熔断"""
```

### 4.2 SshPool

```python
class SshPool:
    def __init__(self, idle_timeout_s=600): ...
    async def device(self, key: SessionKey, access: SshAccess, vendor: str) -> DeviceSession
        # 池命中且 alive→复用；否则新建（to_thread(ConnectHandler)）；每会话一把 asyncio.Lock
    async def close_lab(self, lab_id: str) -> None    # 按 lab_id 归属批量回收会话（lab 销毁时）
    async def host(self, host: str, port: int, user: str, cred: str) -> HostSession   # paramiko
    async def close_all(self) -> None
    def start_keepalive(self, interval_s=30) -> asyncio.Task      # is_alive 探测→重建≤3→1800s 冷却

class DeviceSession:
    lock: asyncio.Lock                                     # 会话内命令串行（设备交互 shell 不支持并发）
    async def exec_cli(self, cli: str, read_timeout_s=120) -> ExecResult     # §5.2
    async def exec_config(self, lines: list[str]) -> ExecResult              # 进配置视图→逐条→回退+归位
    async def capture_config(self) -> str                                    # display current-configuration 全文
    async def close(self) -> None
class HostSession:                                # 宿主 SSH（paramiko exec_command；ADR-25 后仿真平台不用——保留供 carrier 远端主机等复用）
    async def run(self, cmd: list[str], timeout_s=60) -> ExecResult
```

## 5. 内部逻辑

### 5.1 并发模型（网络核实 R5）

netmiko 4.x 无稳定原生 asyncio API（实验性 AsyncConnHandler 不在稳定面）——**同步 netmiko + `asyncio.to_thread` 包裹**，每会话 asyncio.Lock 串行；并发度=不同 (ip,port) 键天然并行（ADR-24：多沙箱实例/多设备端点各不相同时全并行；同一 lab 内共享设备状态，prepare/restore_task 由上层 lab 锁互斥——orchestrator 持有，DD-12）。连接参数：`global_delay_factor=2, fast_cli=False, timeout=30`（nl2cli 实证）。

### 5.2 exec_cli 结果判定（借鉴 nl2cli 逐行锚定）

```
out = to_thread(conn.send_command, cli, expect_string=PROMPT_RE, read_timeout=…)
matched = [p for p in vendor.error_patterns if any(re.match(p, line.strip()) for line in out.splitlines())]
ok = (not matched) and "timed out" not in out
返回 ExecResult(ok, output, matched)      # SSH 成功≠命令成功——两层分离，避免误报
错误模式（vrp）：r"^Error:", r"^Unrecognized command", r"^Invalid input", r"^%.*\^", r"Wrong parameter"
```

### 5.3 prepare（exec_cli 备份→下发；ADR-20）

```
for entry in action.entries:
    s = pool.device((lab.host, entry.device), lab.endpoints[entry.device], vendor=…)
    backup = await s.capture_config()                       # ① 任务前备份
    path = f"{labs_dir}/{lab.lab_id}/backups/{task_id}/{entry.device}.cfg"   # 写文件
    await s.exec_config(entry.cli_lines)                    # ② 下发（§视图管理：进/出配置视图+归位用户视图）
    applied.append(AppliedAction(…, backup_files={dev: path}))
journal.append(lab.lab_id, action)                          # ③ ops.jsonl 追加（L2 重放源）
```

视图归位（借鉴 nl2cli）：`exec_config` 结束统一 `exit_config` + 检测提示符层级，逐层 `quit` 回用户视图（残留视图会污染下一命令）。

### 5.4 restore_task·exec_cli 恢复（按厂商模板，DD 设计 3.4.1）

```
s.exec_config(vendor.reset_cmd)            # vrp: reset saved-configuration + 重启确认（自动 Y 白名单内）
s.exec_config(["save","y"])？否——直接逐行重放 backup 全文（配置式命令幂等 SET 语义）
# 等价性快检：capture_config() 与 backup 规范化 diff（去时间戳/序号行）为空→OK；否则重试≤2
```

### 5.5 call_api（ConfigApiCaller）

```
cfg = env.config_apis[action.api]
body = {"env_name": params.env_name, "scenario": params.scenario,
        "devices": [{device, cli_lines}…], "undo": false}
r = await httpx.post(cfg.base_url + "/apply", json=body, headers=auth(cfg), timeout=cfg.timeout_s)
非 2xx→EnvError（含响应体摘要）；undo 路径：body["undo"]=true（restore_task 用，集成契约：API 幂等）
```

### 5.6 FakeSandboxBackend（离线测试基座）

```
内存设备表 {name: {config_lines: set, running_output 规则}}
exec(cli)→按注入 cli_lines 推导 display 输出（规则表：display current-configuration→已下发命令序列拼接）
build/wait_ready/snapshot(degraded 标记)/restore=重放（与真实现同语义）→BatchRunner/判定 DSL 全离线可测
```

### 5.7 PrebuiltBackend（预建模式，ADR-25）

- **构造**：`PrebuiltBackend(key, kind, probe, ssh_pool, readiness)`（bootstrap 按 settings kind 组装：ensp-pro/pnetlab→Prebuilt，共享全局 SshPool + ReadinessChecker + PlatformProbe；fake→FakeSandboxBackend）；内部 `_envs[lab_id]` 登记；
- **共享 pool 的 (ip,port) 键**：exec 与健康检查与 SandboxService 走**同一共享 SshPool**（SessionKey=(ip,port) 全局唯一，ADR-24）——无双层会话；lab 销毁按 lab_id 批量回收；
- **生命周期**：build=probe 在线校验（离线→EnvError 含平台 url）→ `LabHandle(endpoints=dict(env.ssh_access))`（端点透传，不创建任何对象）；wait_ready=ReadinessChecker（缺省每设备 ssh_reachable TCP 轮询）；snapshot=no-op（SnapRef.impl_kind="noop"）；restore=no-op+warning（L1 配置备份恢复已覆盖任务恢复，L2 退化由 reset 承担）；**reset≠no-op**——逐设备经共享 pool 执行厂商 reset_cmd（vendor.py SSOT）；destroy=pop _envs + `ssh_pool.close_lab(lab_id)`；
- **status()**：覆盖为探活结果——`BackendStatus.available = probe.probe_cached().ok`（TTL 缓存，默认 15s）；`backend_status()` 聚合各后端，LabScheduler select 过滤 available=False（全不可用→EnvError）；
- **两层错误语义**：平台离线（URL 探活失败）→ build 立即 EnvError（含 url 与异常 detail）、BackendStatus.available=False、HealthMonitor `platform:<key>` checker down+system.alert 告警、调度器跳过——**已建 lab 不受影响**（设备 SSH 与平台 Web 无关）；设备 SSH 不可达→wait_ready 轮询超时→EnvError→env_failed、health_check（display clock 登录探测）False，错误定位到设备 (ip,port)。

## 6. 依赖矩阵

- imports：netmiko、httpx（平台 URL 探活 + call_api）、rsi/core、rsi/settings；（预建模式无平台侧编排协议——Web API 编排 P1/S11）。
- 注入：SandboxService←backends+SshPool+settings；无对 carrier/tracing 依赖。
- DB：**写** labs、lab_snapshots；ops.jsonl/备份=文件（labs_dir 下）。
- 被依赖：orchestrator（DD-12 prepare/restore_task/build）、judge（DD-08 exec_readonly）、api（DD-13 labs 路由透传）。

## 7. 测试映射与实施注记

**测试**：TU-023（PrepareAction 编译展开/call_api 请求组装）、TU-030（L1 备份恢复模板+L2 切换）、TU-035（白名单）、IN-012（build_lab 仅 1 次/判决后恢复/prepare 失败重试）、IN-015（超时销毁）、SIM-001/002（真实后端）、EE-002/EE-009 走 Fake；预建模式单测（Mock 探活/MockDeviceSession/假 tcp_probe）：build 端点透传、平台离线 raise、no-op snapshot/restore、reset 命令、destroy 清 pool、scheduler 过滤（§5.7）。

**实施注记**：① 预建模式（ADR-25）——ensp-pro/pnetlab 统一由 PrebuiltBackend 承载（§5.7），无宿主编排 CLI；② PNETLab=同实现 kind=pnetlab（EVE-NG 兼容 REST 编排 P1/S11 评估）；③ 自动确认交互 `[Y/N]` 提示仅限 reset/恢复模板内白名单动作（prepare 命令不自动确认，防误操作）；④ httpx 客户端复用（连接池——探活与 call_api 共用依赖）；⑤ ops.jsonl 行=AppliedAction 序列化（含 applied_at）；⑥ netmiko 同步调用一律 asyncio.to_thread 包裹（§5.1）。
