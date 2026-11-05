# RSI-PG-DD-05 carrier —— SUT 承载适配层

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-05 |
| 版本 | v1.2（v2.18 R1-R6 远端 Docker 主机支持 + G5 镜像验证） |
| 上游 | 设计 3.2、接口 §3/§10；FR1、ADR-22（OpenCode P0 优先）；ADR-4 |
| 交付切片 | S4（HostService+OpenCodeAdapter+Fake+overlay） |

## 1. 追溯与范围

**职责**：SUT 容器实例全生命周期（create→start→submit→collect→destroy，**P0 每 run 一实例**）、执行主机池、双网络隔离、overlay 副本生成、OpenCode 适配器（P0）+ Fake。

**不做什么**：ClaudeCodeAdapter（P2，ADR-22——实施事实已在附 C 预置）；实例池复用（FR1.7 P1）；不做轨迹解析（tracing 职责）。

## 2. 文件与类清单

```
rsi/carrier/__init__.py
rsi/carrier/service.py       # HostService（I-1 服务端）
rsi/carrier/hosts.py         # HostPool：本机 npipe / P1 远端 SSH DOCKER_HOST
rsi/carrier/network.py       # 网络成员资格隔离（P0）+ iptables 白名单（P1）
rsi/carrier/overlay.py       # OverlayBuilder（ADR-4）
rsi/carrier/extract.py       # FinalJsonExtractor（Q6）
rsi/carrier/adapters/__init__.py
rsi/carrier/adapters/base.py     # CarrierAdapter ABC（接口 §10）
rsi/carrier/adapters/opencode.py # OpenCodeAdapter（P0 优先载体）
rsi/carrier/adapters/fake.py     # FakeCarrier
```

## 3. 数据结构

```python
class InstanceHandle(BaseModel):
    instance_id: str; sut_id: str; run_id: str | None
    host: str                       # hosts 键（P0="main"）
    container_id: str; status: InstanceStatus
    config_dir: Path                # export 目录或 overlay 目录（挂载源）
    env: dict[str, str]             # 容器环境变量快照（凭证不入库——仅内存）

class RunHandle(BaseModel):
    run_id: str; instance_id: str; cmd: list[str]
    proc: Any | None = None         # 适配器内部执行句柄（不序列化）

class CarrierImage(BaseModel):      # suts.image_ref 解析 + carriers 配置基底
    image_ref: str; image_base: str | None = None   # 基底镜像=build_image 的 FROM
```

## 4. 对外接口

```python
class HostService:
    def __init__(self, gitstore, overlay: OverlayBuilder, pool: HostPool,
                 network: NetPolicy, adapters: dict[str, CarrierAdapter], settings): ...
    async def create_instance(self, image_ref: str, config_version_id: str,
                              lab: LabHandle, permission: Literal["read-only","read-write"],
                              instrument: bool, host: str = "main",
                              cred: ResolvedCreds | None = None, run_id: str | None = None) -> InstanceHandle
        # ① gitstore.export(version, dest)（to_thread）
        # ② instrument=True → overlay.build(dest, plan=None 先直通；带计划由 instrumentor 预生成传入）
        # ③ docker create（to_thread(docker-py)）：挂载 /agent-config=dest、/trace 卷；
        #    网络=[lab-<id>, trace]（不接 mgmt——成员资格隔离，EE-009）
        # ④ env 注入：DEV_<NAME>_*（ResolvedCreds）+ 载体通道变量（§5.2）
    async def start/stop/destroy(instance) -> None
    async def verify_image(self, image_ref: str) -> list[ImageCheck]        # G5 v1.1
        """docker inspect（存在性）+ docker run --rm <ref> --version（CLI 可执行）
        + 检查 /trace/ 目录（docker run --rm <ref> ls /trace/）
        + 检查 prompt-logger 插件（docker run --rm <ref> opencode plugin list）
        → [{item, passed, detail}]——WEB '验证镜像'按钮数据源"""
    async def submit_task(self, instance, task: TaskSpec) -> RunHandle       # 适配器分发
    async def collect(self, handle: RunHandle, timeout_s: int = 1800) -> TaskResult
        # 超时→abort 容器→TaskResult(task_complete=False)+上层置 sut_timeout
        # 正常退出→检查容器 exit_code（F2 v1.1）：
        #   exit_code 非零（137/OOM、139/segfault、1/error）→ instance_crashed=True
        #     → 不做 FINAL_JSON 提取（无意义），直接返回
        #   exit_code == 0 → 走 FINAL_JSON 提取流程（§5.1）
    async def destroy_stale(self, host: str) -> int                           # DELETE /instances/{id} 支撑

class HostPool:
    """Multi-host Docker client pool — local npipe + remote SSH tunnels (v1.2 R1, v2.18)."""
    def __init__(self, host_configs: dict[str, dict]):
        # host_configs from carrier.hosts: {"main": {docker: local}, "gpu-01": {docker: ssh://user@host}}
        # _clients: dict[str, DockerClient] (lazy creation, cached per host)
        # _instance_counts: dict[str, int] (per-host active instance count)
    def client(self, host: str) -> DockerClient
        # 'main'/local → docker.from_env()；ssh:// → DockerClient(base_url=url)（缓存）
    async def check_reach(self, host: str) -> bool     # client(host).ping() 探测
    async def statuses(self) -> list[HostStatus]       # 含 reachable + instance_count + max_instances
    def increment(self, host: str) / decrement(self, host: str)  # 调度器计数

class InstanceScheduler:
    """New instance host selection — least-loaded + max_instances_per_host (v1.2 R2, v2.18)."""
    def __init__(self, host_pool: HostPool, max_per_host: int = 10): ...
    def select(self, explicit_host: str | None = None) -> str
        # 显式指定→用之；否则按 instance_count 升序选首个 count<max 的主机；全满→CarrierError

class NetPolicy:
    """Per-host network management (v1.2 R3, v2.18) — 每 host 独立 Docker daemon/网络命名空间."""
    def __init__(self, host_pool: HostPool): ...
    async def ensure_lab_net(self, host: str, lab_id: str) -> str   # 在指定 host 上创建 lab-<id>
    async def ensure_trace_net(self, host: str) -> str              # 在指定 host 上创建 trace
    async def attach(self, host: str, container_id: str, networks: list[str]) -> None
    # P1：async def egress_rules(container, allowed_subnets, ports=[22,830])

class OverlayBuilder:                                            # ADR-4
    def build(self, config_dir: Path, plan: InstrumentPlan | None) -> Path
        # copy-on-write 复制 → plan 应用（DD-06 定义条目）→ overlay.json（来源 version/plan 摘要/destroyable）
```

## 5. 内部逻辑

### 5.1 collect 流程与 FINAL_JSON 提取（Q6，TU-029）

```
out = await adapter.collect_result(handle, timeout)   # stdout 全文（--print-logs 在 stderr 不混入）
extract_task_result(out)：
  1) 优先匹配最后一个 <FINAL_JSON>…</FINAL_JSON> 括号段 → json.loads
  2) 否则取输出末尾最后一个完整 JSON 对象（平衡括号扫描——借鉴 nl2cli salvage）
  3) 映射 TaskResult（task_complete/root_cause/actions/confidence）；失败→parse_error=True
```

### 5.2 OpenCodeAdapter（P0）

```python
class OpenCodeAdapter(CarrierAdapter):
    async def build_image(self, sut_def) -> str:
        # FROM carriers.adapters.opencode.image_base（rsi/opencode-runtime:1.x）
        # 预装：opencode CLI + opencode-prompt-logger 插件（opencode plugin add 离线包）
    async def start(self, instance, config_mount, env) -> None
        # 生成 opencode.json（镜像内 ~/.config/opencode/opencode.json）：
        #   provider/model（model.json 翻译，FR1.8）+ plugins: {"opencode-prompt-logger": {
        #     "file": f"/trace/wiretap/{run_id}.ndjson",      ← per-run 归属（附 C.3）
        #     "rotateBytes": false, "http": true}}
        #   env 注入 OPENCODE_…（provider API key 经 CredentialStore→env）
    async def submit_task(self, instance, task) -> RunHandle:
        # cmd = ["opencode","run","--format","json","--print-logs","--session",run_id, task.prompt]
        # 长文本经 stdin
    async def collect_result(self, handle, timeout_s) -> str      # stdout 原文返回
    def trace_channels(self, instance) -> TraceChannels:
        # {kind:"ndjson", paths:["/trace/wiretap/*.ndjson", "/trace/events.ndjson"]}
        # 注意 stdout 重定向截断已知问题（#29866）→ 轨迹不依赖 stdout（附 C.4）
```

### 5.3 每实例生命周期（P0 每 run 一实例）

```
create_instance（挂载/网络/env）→ start → submit → collect
  → destroy（实例销毁；overlay 随之回收——ADR-4；wiretap/events 文件在 /trace 卷持久，collector 已尾随）
异常路径：collect 超时→abort→destroy；create 失败→清理半成品容器+目录
```

## 6. 依赖矩阵

- imports：docker（docker-py）、rsi/core、rsi/settings；（版本管理调用 gitstore 接口 DD-09）。
- 注入：HostService←GitStore+OverlayBuilder+HostPool+NetPolicy+adapters。
- DB：**写** runs.instance_id（经 orchestrator）；容器/docker 无表。
- 被依赖：orchestrator（DD-12 create/submit/collect）、instrumentor（overlay 消费）、api（instances 路由→HostService.destroy_stale）。

## 7. 测试映射与实施注记

**测试**：TU-017（overlay copy-on-write/幂等标记/原始目录零改动）、TU-029（FINAL_JSON 两策略/失败路径）、EE-001（OpenCode 示例 SUT 双配置版本可区分轨迹）、EE-009（隔离：容器内 curl 平台 API 端口失败）、IN-004/005（经 Fake/injector）。

**实施注记**：① docker-py 同步→`asyncio.to_thread` 统一包裹；② `rsi-<instance_id>` 容器命名（2.5.C）；③ hosts.py P0 仅 main（`docker.from_env()` Windows npipe），remote 分支 P1 实现留桩；④ opencode.json 写入挂载的 /agent-config（随 overlay/export 走，不烧镜像）；⑤ prompt-logger 离线安装：镜像构建期 `opencode plugin add` 或 vendored 包路径（部署脚本 10.2）；⑥ FakeCarrier：submit→cassette 指定任务结果 stdout（含合法 FINAL_JSON 与缺陷变体两种 fixture），trace_channels 返回内存事件列表。
