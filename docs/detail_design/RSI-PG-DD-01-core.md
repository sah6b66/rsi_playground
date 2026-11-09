# RSI-PG-DD-01 core —— 领域模型 / 枚举 / 命名 / 异常 / 数据库

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-01 |
| 版本 | v1.1 |
| 修订 | v1.1（2026-11-09）第四轮审计文档-代码对齐：§3.11 RunStatus 转换表补 env_ready→aborted、collecting→aborted（D-1） |
| 上游 | 设计 2.4/2.5、接口 §2.1/§2.2；FR 全部（数据基座） |
| 交付切片 | S1（一切的地基） |

## 1. 追溯与范围

**职责**：为全部模块提供纯数据层——Pydantic 领域模型、状态枚举、命名生成/校验、异常体系、DB 引擎与 SQLModel 表映射。**纯数据 + 纯函数，无 IO、无网络、无 LLM**（唯一例外：db.py 建引擎）。

**不做什么**：不含业务流程（各模块 DD）；不含配置（settings.py 属 S1 但见配置文档）；表结构以接口 §2.2 DDL 为权威，本文定义映射约定而非复述 DDL。

## 2. 文件与类清单

```
rsi/core/__init__.py        # 导出全部公共模型
rsi/core/models.py          # 领域模型（§3.1~3.10）
rsi/core/enums.py           # 全部枚举（§3.11）
rsi/core/naming.py          # ID 生成与校验（§4.1）
rsi/core/errors.py          # 异常体系（§3.12）
rsi/db.py                   # 引擎/会话工厂/init_db（§4.2）
rsi/db/__init__.py
rsi/db/tables.py            # SQLModel 表（22 张，§3.13 映射矩阵）
```

## 3. 数据结构

### 3.1 评估集三件套（models.py）

```python
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal, Any

class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

class SshAccess(_M):
    ip: str                          # IPv4/IPv6
    port: int = 22                   # ge=1 le=65535
    username: str                    # min_length=1
    password: str | None = None      # 与 password_ref 二选一（model_validator 强制）
    password_ref: str | None = None  # CredentialStore 名

class DeviceSpec(_M):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")   # 设备名（任务/断言引用键）
    type: Literal["vrp", "ios", "ce"]                              # → netmiko device_type（DD-04）
    version: str | None = None
    config: str                                                     # configs/ 下相对路径，如 "configs/R1.cfg"

class LinkSpec(_M):
    endpoints: tuple[str, str]       # "R1:GE0/0/1" 形式，两端各一
    type: Literal["ethernet", "serial"] = "ethernet"

class Topology(_M):
    links: list[LinkSpec] = []

class ReadyCheck(_M):
    type: Literal["ssh_reachable", "cli_match"]
    device: str | None = None        # cli_match 必填（校验器）
    cli: str | None = None
    expect: str | None = None        # cli_match 必填

class ConfigApi(_M):                 # 外部配置 API 端点（ADR-20 call_api 手段）
    base_url: str                    # http(s)://…
    auth_ref: str | None = None      # CredentialStore 名；None=无鉴权
    timeout_s: int = 60

class ExternalDep(_M):               # P2 NMS 端点声明
    kind: Literal["nms"]
    base_url: str
    auth_ref: str | None = None

class EnvSpec(_M):
    name: str                        # 组网名
    backend: Literal["ensp-pro", "pnetlab", "containerlab", "fake"]
    devices: list[DeviceSpec]        # min_length=1；name 唯一（校验器）
    topology: Topology
    ssh_access: dict[str, SshAccess] # 设备名 → 接入；keys == devices.name（校验器）
    ready_checks: list[ReadyCheck] = []    # 缺省=全部设备 ssh_reachable
    config_apis: dict[str, ConfigApi] = {}
    external_deps: list[ExternalDep] = []
    # 站点覆盖合并：apply_site_override(site: EnvSpec) -> EnvSpec
    #   仅允许覆盖 backend 与 ssh_access（其余字段不一致 → SiteOverrideError）

class CliEntry(_M):                  # exec_cli 单设备条目
    device: str
    cli_lines: list[str] = Field(min_length=1)

class CallApiParams(_M):             # call_api 参数（ADR-20）
    env_name: str                    # 组网名
    scenario: str                    # 评测场景名
    devices: list[CliEntry] = Field(min_length=1)

class PrepareAction(_M):
    action: Literal["exec_cli", "call_api"]
    entries: list[CliEntry] | None = None      # exec_cli 必填
    api: str | None = None                      # call_api 必填：env.config_apis 键
    params: CallApiParams | None = None         # call_api 必填

class TaskSpec(_M):
    prompt: str                      # 任务描述（含 FINAL_JSON 约定文案）
    context: dict[str, Any] = {}     # 附加上下文（拓扑摘要等）
    tool_constraints: list[str] = [] # 可用工具约束

class RootCauseExpectation(_M):     # ADR-23：自然语言期望 + 可选提示
    expected: str                    # 自然语言根因描述（LLMJudge 语义比对）
    match_hints: list[str] = []      # 仅报告高亮/预筛，不作判据

class StateAssert(_M):
    device: str                      # 设备名
    cli: str                         # 只读命令（前缀白名单校验在 sandbox）
    expect_contains: list[str] = []  # 与 expect_absent 至少一项（校验器）；多值 AND
    expect_absent: list[str] = []

class CheckItem(_M):                 # 巡检项
    item: str                        # 检查内容描述
    evidence_cli: str | None = None  # 期望的证据命令（可作 critical_steps 补充）

class RiskItem(_M):                  # 预检风险项
    risk: str                        # 风险描述
    severity: Literal["high", "medium", "low"] = "medium"

class CriticalStep(_M):
    evidence: str                    # 期望出现的命令片段（tool_call 字符串匹配）

class ExpectedAnswer(_M):
    root_cause: RootCauseExpectation | None = None
    final_state: list[StateAssert] = []
    checklist: list[CheckItem] | None = None       # 巡检集
    risk_findings: list[RiskItem] | None = None    # 预检集
    critical_steps: list[CriticalStep] | None = None

class MetricDef(_M):
    name: Literal["root_cause_accuracy", "task_complete", "diagnostic_steps",
                  "missed_check", "false_positive", "tokens", "tool_calls", "wall_time_s"]

class TaskRecord(_M):                # tasks.jsonl 一行
    task_id: str = Field(pattern=r"^(fault|inspection|cutover)/[a-z0-9-]+-\d{2}$")
    title: str
    prepare: list[PrepareAction] = []
    task: TaskSpec
    expected: ExpectedAnswer
    metrics: list[MetricDef] = []
```

### 3.2 运行时视图与轨迹

```python
class EvalTask(_M):                  # 运行时配对：EnvSpec × TaskRecord
    env: EnvSpec
    task: TaskRecord
    # 便捷方法：resolve_device(name) -> SshAccess（ KeyError→UnknownDeviceError ）

class TraceEvent(_M):
    run_id: str; task_id: str; lab_id: str; sut_id: str   # sut_id=纯标识（版本经 run 反查）
    seq: int = Field(ge=0); ts: str                        # UTC ISO8601 毫秒
    event_type: "EventType"                                # §3.11
    model: str | None = None
    data: dict[str, Any] = {}          # llm_request: {system,messages,tools,usage{input_tokens,output_tokens,estimated?}}
    duration_ms: int | None = None
    session_id: str | None = None; step_seq: int | None = None
    late: bool = False                 # finalize 宽限后到达标记（v2.3 T3）

class WiretapFrame(_M):               # 载体原生帧（解析器输入，DD-07）
    carrier: Literal["opencode", "claude_code"]
    kind: str                         # opencode: assembled/http；cc: system/assistant/user/result
    raw: dict[str, Any]               # 原帧 JSON（保留全文）
    at: str                           # 帧内时间戳
```

### 3.3 判决

```python
class Scores(_M):
    root_cause_accuracy: float = Field(ge=0, le=1)   # LLMJudge 语义判定
    task_complete: bool
    diagnostic_steps: int = Field(ge=0)               # critical_steps 命中数
    missed_check: int = Field(ge=0); false_positive: int = Field(ge=0)
    indeterminate: bool = False                       # LLM 证据不足/失败

class PathQuality(_M):
    verdict: Literal["pass", "suspect"]
    skipped_critical_steps: list[str] = []
    reasoning: str

class Efficiency(_M):
    tokens: int = 0; tool_calls: int = 0; wall_time_s: float = 0.0
    estimated: bool = False                           # token 为字符估算时 True（附 C.5⑥）

class SafetyVerdict(_M):
    violations: list[str] = []        # 命中规则描述
    fatal: bool = False; major: int = 0; minor: int = 0

class Verdict(_M):                    # 需求 3.6 代码化
    scores: Scores; path_quality: PathQuality
    efficiency: Efficiency; safety: SafetyVerdict
    reward_hacking_suspect: bool = False
    missing: bool = False             # 缺试（保留分母，聚合层消费）
```

### 3.4 假设与归因

```python
class ProblemStatement(_M):           # DGM 问题单（FR6.3 stage1）
    cluster_id: str; failure_mode: str
    defect_class: Literal["A", "B"]
    evidence_trace_ids: list[str]     # run_id 引用
    evidence_sut_refs: list[str] = [] # 原文证据引用 "suts/<id>/<path>#L<行>"（v2.10）
    suspected_elements: list[str]     # 文件路径（假设生成取原文）
    problem_text: str

FAILURE_MODES = Literal["call_order_error", "missed_critical_command", "reasoning_skip",
    "causal_misattribution", "hallucinated_command", "alert_not_condensed",
    "yang_path_error", "knowledge_missing", "output_unparseable", "other"]

class Hypothesis(_M):                 # FR6.3
    problem_trace_ids: list[str]
    failure_type: FAILURE_MODES | str
    defect_class: Literal["A", "B"]
    proposal_type: Literal["modify_prompt", "add_runbook", "adjust_tool_sequence",
        "add_fewshot_example", "add_knowledge", "switch_model", "custom"]
    diff_content: str | None = None          # 与 model_config_patch 二选一
    model_config_patch: dict[str, Any] | None = None
    expected_improve: str
    risk: Literal["low", "medium", "high"]
    verification_task_ids: list[str]
    predicted_affected_tasks: list[str]

class AttributionCluster(_M):
    failure_mode: str; defect_class: Literal["A", "B"]
    cluster_size: int; sample_run_ids: list[str]
    evidence: list[str]               # 轨迹引用 + SUT 原文证据引用（v2.10）

class AttributionReport(_M):
    clusters: list[AttributionCluster]        # 按 簇大小×可优化性 降序
```

### 3.5 优化配置（OptimizationProfile，配置文档 §3 全字段）

```python
class Goal(_M):
    metric: str; direction: Literal["max", "min"]; weight: float = Field(gt=0)

class EvalsetBinding(_M):
    iterate: str; holdout: str; smoke: str          # evalset_id

class GuardPolicy(_M):
    missed_check_worsen: Literal["reject"] = "reject"
    safety_any_worsen: Literal["reject"] = "reject"
    high_risk_worsen: Literal["reject"] = "reject"

class StopPolicy(_M):
    max_rounds: int = Field(default=20, ge=1)
    no_gain_rounds: int = Field(default=5, ge=1)

class Budget(_M):
    max_tokens: int = Field(gt=0); max_wall_hours: float = Field(gt=0)
    max_cost_usd: float | None = None

class LlmCompare(_M):
    candidates: list[str] = []

class OptimizationProfile(_M):
    name: str
    evalsets: EvalsetBinding
    goals: list[Goal] = Field(min_length=1)
    enabled_targets: set[Literal["system_prompt","skills","scripts","mcp_config",
        "mcp_src","memory","knowledge","model_config"]] = Field(min_length=1)
    guards: GuardPolicy = GuardPolicy()
    stop: StopPolicy = StopPolicy()
    budget: Budget | None = None                    # None=用 orchestrator.budget 默认
    llm_compare: LlmCompare | None = None
```

### 3.6 SUT 任务结果（Q6）

```python
class ActionRecord(_M):
    action: str                       # 如 "display bgp peer"
    detail: str | None = None

class TaskResult(_M):
    task_complete: bool
    root_cause: str | None = None
    actions: list[ActionRecord] = []
    confidence: float | None = None
    parse_error: bool = False         # FINAL_JSON 提取失败（A 类"输出不可解析"样本）
    instance_crashed: bool = False    # F2：容器非零退出（OOM/segfault）——infra 故障，排除归因
```

### 3.7 LLM 层（DD-02 消费）

```python
class LLMUsage(_M):
    input_tokens: int = 0; output_tokens: int = 0
    estimated: bool = False

class LLMResponse(_M):
    content: str                      # 原文
    parsed: Any | None = None         # schema 校验后的结构化输出
    usage: LLMUsage = LLMUsage()
    model: str; prompt_version: str
    finish_reason: str | None = None
```

### 3.8 沙箱句柄与操作记录

```python
class LabHandle(_M):
    lab_id: str; host: str; backend: str
    endpoints: dict[str, SshAccess]   # 设备名 → 已验证端点（ADR-24 三级解析：
                                       # 适配器 (网络名,设备名)→(IP+端口) 映射权威；env.ssh_access 基准+凭证）

class AppliedAction(_M):              # prepare 执行记录（restore_task 输入 / ops.jsonl 行）
    action: Literal["exec_cli", "call_api"]
    entries: list[CliEntry] | None = None
    api: str | None = None; params: CallApiParams | None = None
    backup_files: dict[str, str] = {} # exec_cli：设备名 → backups/<task>/<dev>.cfg 路径
    applied_at: str

class SnapRef(_M):
    snapshot_id: str; impl_kind: Literal["native", "degraded"]; backend_ref: str | None
```

### 3.9 决策（DD-10 消费）

```python
class BatchStats(_M):
    metrics: dict[str, float]         # metric → mean
    n: int; missing: int              # 缺试计入分母（missing/n 供规则 0）
    tokens: int; tool_calls: int; wall_time_s: float; tokens_estimated: bool

class Decision(_M):
    action: Literal["PROMOTE", "REVERT", "REJECT", "ADVISE_MANUAL"]
    rule_hit: int                      # 命中规则序号 0-6
    reason: str
    evidence: dict[str, Any]           # 两批 batch_id/指标 delta/检验结果
```

### 3.10 迭代与门

```python
class IterationSnapshot(_M):          # GET /iterations/{id} 响应体
    id: str; sut_id: str; mode: Literal["p1", "p2"]
    state: "IterationState"; current_step: str; round: int
    experiments: list[str]
    budget_used: dict[str, Any]       # {tokens, cost_usd, wall_time_s}
```

### 3.11 enums.py（全部枚举与状态机转换表）

```python
class BatchStatus(StrEnum):      CREATED="created"; RUNNING="running"; JUDGING="judging";
                                 FINALIZED="finalized"; FAILED="failed"; ABORTED="aborted"
class RunStatus(StrEnum):        PENDING="pending"; ENV_BUILDING="env_building"; ENV_READY="env_ready";
                                 SUT_RUNNING="sut_running"; COLLECTING="collecting"; JUDGING="judging";
                                 DONE="done"; ENV_FAILED="env_failed"; SUT_TIMEOUT="sut_timeout";
                                 ABORTED="aborted"; CACHED="cached"
class LabStatus(StrEnum):        BUILDING="building"; READY="ready"; FAULTED="faulted";
                                 DESTROYED="destroyed"; ERROR="error"
class EventType(StrEnum):        LLM_REQUEST="llm_request"; LLM_RESPONSE="llm_response";
                                 TOOL_CALL="tool_call"; TOOL_RESULT="tool_result";
                                 SKILL_LOADED="skill_loaded"; KNOWLEDGE_QUERY="knowledge_query";
                                 SCRIPT_EXEC="script_exec"; ERROR="error"; TASK_END="task_end"
class HypothesisStatus(StrEnum): DRAFT="draft"; PROPOSED="proposed"; CRITIC_PASSED="critic_passed";
                                 APPLIED="applied"; ACCEPTED="accepted"; REJECTED="rejected";
                                 WITHDRAWN="withdrawn"
class IterationState(StrEnum):   ATTRIBUTING; HYPOTHESIZING; REVIEWING; APPLYING; VERIFYING;
                                 DECIDING; PAUSED; ABORTED; ARCHIVED; FINISHED
class InstanceStatus(StrEnum):   CREATING; RUNNING; STOPPED; DESTROYED; FAILED
class ExperimentDecision(StrEnum): PROMOTED="promoted"; REVERTED="reverted";
                                 PENDING="pending"; ADVISE_MANUAL="advise_manual"
```

**状态机转换表**（非法转换→InvalidTransitionError，TU-001/TU-027 消费）：

```
BatchStatus:  created→running→judging→{finalized|failed}; running/judging→aborted
RunStatus:    pending→env_building→env_ready→sut_running→collecting→judging→done
              env_building→env_failed; sut_running→sut_timeout|aborted; pending→cached
              env_ready→aborted; collecting→aborted
              # abort 语义允许运行期任意活跃态中止（与 enums.py 实现一致）
Hypothesis:   draft→proposed→critic_passed→applied→{accepted|rejected}; 任意→withdrawn（人工）
Iteration:    ATTRIBUTING→HYPOTHESIZING→REVIEWING→APPLYING→VERIFYING→DECIDING→ATTRIBUTING(下一轮)|FINISHED
              任意运行态→PAUSED→原状态；任意→ABORTED；FINISHED/ABORTED→ARCHIVED（软删）
```

### 3.12 errors.py（异常体系）

```python
class RsiError(Exception):            # 基类：message + context: dict
class ConfigError(RsiError)
class NamingError(RsiError)           # 含 SiteOverrideError
class EnvError(RsiError)              # 环境构建/准备/恢复失败
class DeviceError(RsiError)           # SSH/命令失败（含 output、device、cli）
class CarrierError(RsiError)
class TraceError(RsiError)
class JudgeError(RsiError)            # 含 indeterminate 语义由 Verdict 表达，不抛
class LLMOutputInvalid(RsiError)      # schema 校验 retry 耗尽（ADR-1/7 → 降级人工）
class GitStoreError(RsiError)         # 含 FreezeViolationError、NotFastForwardError
class TargetViolationError(RsiError)  # 白名单/冻结域越界（path、target）
class BudgetExceeded(RsiError)
class AuthError(RsiError)
class InvalidTransitionError(RsiError)# (from_state, to_state)
class UnknownDeviceError(RsiError)
```

对外映射：`to_http(exc) -> (status, code)` 按 §9.0 错误码表（api 层实现，DD-13）。

### 3.13 db/tables.py（22 张表映射）

约定（全部 SQLModel 类）：表名/列名/约束**逐列对照接口 §2.2 DDL（权威）**；`id TEXT PRIMARY KEY`；时间戳 `str`（UTC ISO）；JSON 列 `str`（SQLModel `Field(sa_column=Column(Text))`，读写经模块自有序列化）；FK 用 `Field(foreign_key=True)`。

| 表名 | SQLModel 类 | 所属分组 | 特殊约束 |
|---|---|---|---|
| users / credentials / audit_log | User / Credential / AuditLogRow | 安全 | role/kind CHECK |
| suts / config_versions | Sut / ConfigVersion | 版本 | baseline_version_id 自引用 |
| evalsets / eval_tasks | EvalSet / EvalTask | 评估集 | UNIQUE(evalset_id, task_id) |
| optimization_profiles / iterations | OptimizationProfileRow / Iteration | 优化（**前移组，DD-09 依赖序**） | UNIQUE(sut_id,name)；mode CHECK |
| labs / lab_snapshots | Lab / LabSnapshot | 沙箱 | impl_kind CHECK |
| batches / runs / instrumentation_reports | Batch / Run / InstrumentationReport | 执行 | purpose/permission/status CHECK |
| trace_files / trace_events_index | TraceFile / TraceEventIndex | 轨迹 | 复合主键(run_id,seq) |
| judgments | JudgmentRow | 判决 | — |
| hypotheses / experiments | HypothesisRow / Experiment | 优化 | iteration_id FK |
| noise_bands | NoiseBand | 决策 | UNIQUE 三元组 |
| llm_calls / checkpoints | LlmCall / CheckpointRow | 记账/断点 | — |

## 4. 对外接口

### 4.1 naming.py

```python
def new_id(prefix: str) -> str:
    """生成 '<prefix>-<YYYYMMDDHHmmss UTC>-<8位Crockford base32>'；随机段=secrets.token 驱动。
    合法前缀白名单：batch/run/lab/snap/iter/jd/hyp/exp/inst（对应 2.5.C）。"""
PREFIXES: frozenset[str]
def validate_id(prefix: str, value: str) -> None      # NamingError；TU-022
def validate_sut_id(v: str) -> None                   # ^[a-z][a-z0-9-]{2,31}$
def validate_evalset_id(v: str) -> None               # <域>-<holdout|iterate|smoke|general>
def validate_task_id(v: str) -> None                  # <类别>/<现象>-两位序号
def git_refs(sut_id: str, n: int) -> tuple[str, str]  # ('suts/<id>/baseline/vN', 'suts/<id>/cand/<hyp>')
```

### 4.2 db.py

```python
def create_engine(url: str) -> Engine
    # sqlite: 连接后执行 PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000
    #         —— v2.14 多批次并行写不报 database is locked，读写不互斥
def get_session() -> Iterator[Session]               # FastAPI 依赖（DD-13）
def init_db(engine, admin_user: str, admin_password_hash: str) -> None
    # SQLModel.metadata.create_all（按依赖拓扑自动排序建表——DD-09 手工顺序仅 DDL 文档语义）
    # 幂等；种子 admin 用户（首次）
async def run_db(fn: Callable[[Session], T]) -> T    # asyncio.to_thread 包裹会话操作（全局约定）
```

## 5. 内部逻辑（关键算法）

**ID 生成**（naming.py）：
```
ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
rand = base32(secrets.randbits(40)).lower()[:8]      # 去除 i l o u 的 Crockford 字母表
return f"{prefix}-{ts}-{rand}"
```

## 6. 依赖矩阵

- imports：pydantic、sqlmodel、sqlalchemy、stdlib（secrets/datetime/re）；**不 import 任何兄弟模块**。
- DB：无专属表；提供全部表定义。
- 被依赖：全部模块；测试 TU-016/022/033（模型校验器）等。

## 7. 测试映射与实施注记

- TU-022（命名正反例）、模型校验器单测随 S1 编写（password/password_ref 二选一、ssh_access 键覆盖、StateAssert 至少一断言、eval_tasks 唯一等——建议新增 TU-036"领域模型校验器"）。
- 实施注记：① Pydantic v2 `model_validator` 实现跨字段约束；② frozen=True 使模型可哈希可安全传递；③ SQLModel P0 用同步引擎+`run_db(to_thread)`（避免 aiosqlite 双栈复杂度，P1 PG 换连接串即可）；④ `trace_events_index` 复合主键在 SQLModel 用 `__table_args__=(PrimaryKeyConstraint('run_id','seq'),)`。
