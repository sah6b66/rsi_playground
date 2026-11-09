# 网络运维 Agent 的 RSI 演练场 接口定义文档

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DES-003 |
| 版本 | v2.6（第四轮审计对齐） |
| 日期 | 2026-11-03 |
| 状态 | 待评审 |
| 上游文档 | 需求 RSI-PG-REQ-001 v0.25（第 3 章：I-1~I-11 需求级契约）；设计 RSI-PG-DES-001 v2.12 |
| 关联文档 | 配置设计 RSI-PG-DES-002（docs/_rsi_playground_configuration.md）；测试设计 RSI-PG-DES-004（docs/_rsi_playground_test.md） |

> 定位：需求文档第 3 章定义**需求级**接口契约（I-1~I-11），本文档给出**实现级**详细定义（服务签名 / 数据契约 / REST 端点）。主设计文档各子系统章节保留职责、内部组件与流程，接口签名统一引用本文档。

---

## 1. 接口总览

| I 编号 | 接口 | 本文档章节 | 主设计文档模块章节 | 类型 |
|--------|------|-----------|--------------------|------|
| — | 数据契约（领域模型 / TaskResult / 元数据库 DDL） | §2.1 / §2.2 | 2.4 / 2.1（引用） | Pydantic + SQL |
| I-1 | SUT-Host（HostService） | §3 | 3.2 | 进程内服务（REST 可包覆） |
| I-3 | Sandbox-Control（SandboxService/SandboxBackend） | §4 | 3.4 | 进程内 + 后端 ABC |
| I-4 | Trace（TraceStore/TraceDiffer） | §5 | 3.5 | 进程内服务 |
| I-5 | Judge（JudgeService） | §6 | 3.6 | 进程内服务 |
| I-6 | Version（GitStore） | §7 | 3.8 | 进程内服务 |
| I-8 | 插桩外部契约（pg-mcp-proxy / pg_trace / 事件文件） | §8 | 3.3 | 容器内代理 + SDK |
| I-9 | Platform API（REST + SSE/WS） | §9 | 3.10 | REST/流 |
| I-10 | 载体适配器（CarrierAdapter + TaskResult） | §10 | 3.2.2 | 插件 ABC |
| — | 编排服务接口（BatchRunner 等） | §11 | 3.1 | 进程内服务 |
| — | 优化与决策服务接口 | §12 | 3.7 / 3.9 | 进程内服务 |
| — | LLM 客户端与提示词库 | §13 | 3.12 | 进程内基础设施 |

（I-2 设备数据面、I-7 EvalSet、I-11 网管北向为协议型接口，契约见需求文档 3.3/3.8/3.12，本文件不重复。）

---

## 2. 数据契约

### 2.1 领域模型（rsi/core/models.py，Pydantic v2）

仅列关键字段与约束（完整定义随代码生成产出，此处为契约）：

```python
class EnvSpec(BaseModel):          # 环境定义（env.yaml；站点覆盖 env.<site>.yaml 仅可覆盖 backend 与 ssh_access）
    name: str
    backend: Literal["ensp-pro", "pnetlab", "containerlab", "fake"]
    devices: list[DeviceSpec]          # name/type/version/config（数据集内相对路径）
    topology: Topology                 # links: [{endpoints: ["R1:GE0/0/1", "R2:GE0/0/1"], type: ethernet|serial|…}]
    ssh_access: dict[str, SshAccess]   # 设备名 → {ip, port, username, password | password_ref(凭证引用)}
    ready_checks: list[ReadyCheck] = []    # 缺省 = 全部设备 SSH 可达
    config_apis: dict[str, ConfigApi] = {} # 外部配置 API（ADR-20）：name → {base_url, auth_ref}；实现在外部系统
    external_deps: list[ExternalDep] = []  # P2: NMS 端点声明（I-11）

class TaskRecord(BaseModel):       # 任务集（tasks.jsonl 一行；设备一律以名称引用，不含 IP）
    task_id: str; title: str
    prepare: list[PrepareAction] = []   # 评测准备（ADR-20 手段两种，均多设备多命令；目的=故障注入/配置修改等语义，由命令内容体现）：
                                        #   {action: exec_cli, entries: [{device, cli_lines: [..]}, …]}
                                        #   {action: call_api, api: <config_apis 键>,
                                        #    params: {env_name 组网名, scenario 评测场景名, devices: [{device, cli_lines: [..]}, …]}}
    task: TaskSpec                      # prompt(任务描述)/context/tool_constraints
    expected: ExpectedAnswer
    metrics: list[MetricDef]
    class ExpectedAnswer(BaseModel):
        root_cause: RootCauseExpectation | None  # {expected: 自然语言根因描述, match_hints?: [关键词]}
                                                 # 语义判定由 LLMJudge 执行（ADR-23）；hints 仅报告高亮/预筛，不作判据
        final_state: list[StateAssert]          # device(名称)+cli+expect_contains+expect_absent（确定性判定 DSL）
        checklist: list[CheckItem] | None       # 巡检集（语义命中判定，LLMJudge）
        risk_findings: list[RiskItem] | None    # 预检集（同上）
        critical_steps: list[CriticalStep] | None  # 证据命中计数 + 偷步检测（FR7.4，字符串匹配）

# 运行时视图：EvalTask = EnvSpec × TaskRecord
#   BatchRunner 逐任务展开（env: EnvSpec, task: TaskRecord）；
#   评测缓存键 = task_content_hash + env_content_hash（主设计 ADR-5；
#   env 哈希计算时剥离 ssh_access 段——站点覆盖切换不使缓存失效）
#   设备名 → SSH 端点解析：env.ssh_access[device]（平台侧与 SUT 侧同源）

class TraceEvent(BaseModel):       # 需求 3.5 Schema 的代码化
    run_id: str; task_id: str; lab_id: str; sut_id: str   # sut_id=纯 SUT 标识（版本经 run→batch 反查）
    seq: int; ts: datetime; event_type: EventType
    model: str | None = None; data: dict = {}; duration_ms: int | None = None
    session_id: str | None = None; step_seq: int | None = None

class Verdict(BaseModel):          # 需求 3.6 Verdict 的代码化
    scores: Scores; path_quality: PathQuality
    efficiency: Efficiency         # 含 estimated 标记（token 为字符估算时透传，3.5 注记/附 C.5⑥）
    safety: SafetyVerdict
    reward_hacking_suspect: bool = False

class Hypothesis(BaseModel):       # FR6.3
    problem_trace_ids: list[str]; failure_type: FailureType | str
    defect_class: Literal["A", "B"]           # A/B 缺陷标注（FR6.9）
    proposal_type: Literal["modify_prompt","add_runbook","adjust_tool_sequence",
        "add_fewshot_example","add_knowledge","switch_model", ...]
    diff_content: str | None; model_config_patch: dict | None
    expected_improve: str; risk: Literal["low","medium","high"]
    verification_task_ids: list[str]; predicted_affected_tasks: list[str]   # 均为 task_id

class OptimizationProfile(BaseModel):         # FR6.1；完整 Schema 见配置文档 §3（全字段持久化）
    name: str
    evalsets: EvalsetBinding                  # {iterate, holdout, smoke} 三集绑定（FR3.7）
    goals: list[Goal]                         # metric/direction/weight（加权综合分见 §12 Selector）
    enabled_targets: set[OptTarget]           # 枚举=白名单八项
    guards: GuardPolicy                       # 非补偿守卫开关（FR9.3）
    stop: StopPolicy                          # max_rounds / no_gain_rounds（FR9.4）
    budget: Budget                            # token/时长/费用上限（FR5.6）
    llm_compare: LlmCompare | None = None     # switch_model 候选模型池（FR6.3/FR1.8）

class TaskResult:                  # SUT 任务结构化输出（Q6 约定，见 §10）
    task_complete: bool; root_cause: str | None
    actions: list[ActionRecord]; confidence: float | None
```

### 2.2 元数据库（SQLite DDL，P0）

> 命名约定：表名复数、id 为 UUID 字符串、时间戳存 UTC ISO8601。SQLModel 映射类同名，与 §2.1 领域模型同源（SQLModel 类即 ORM）——元数据库是全部模块共享读写的**持久化数据契约**。

```sql
-- 用户与安全（NFR8）
CREATE TABLE users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','agent_engineer','researcher','approver','service')),
  password_hash TEXT NOT NULL, disabled INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE credentials (                    -- FR1.5 凭证存储（Fernet 加密）
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('device_ssh','nms_api','llm_api','backend_ssh')),
  cipher_blob BLOB NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE audit_log (                      -- NFR1④
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL,
  object_id TEXT NOT NULL, detail_json TEXT
);

-- SUT 与版本（FR1/FR8）
CREATE TABLE suts (
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  carrier TEXT NOT NULL CHECK(carrier IN ('claude_code','opencode','hermes','custom')),
  image_ref TEXT NOT NULL, description TEXT DEFAULT '',
  baseline_version_id TEXT,                  -- 当前基线（config_versions.id 外键）
  created_at TEXT NOT NULL
);
CREATE TABLE config_versions (
  id TEXT PRIMARY KEY, sut_id TEXT NOT NULL REFERENCES suts(id),
  git_ref TEXT NOT NULL,                     -- Git commit sha
  label TEXT NOT NULL,                       -- 如 v3 / cand-hypo-42
  kind TEXT NOT NULL CHECK(kind IN ('baseline','candidate','archive')),
  parent_id TEXT REFERENCES config_versions(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL
);

-- 评估集（FR3）
CREATE TABLE evalsets (
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('iterate','holdout','generalization','smoke')),
  git_ref TEXT, content_hash TEXT,           -- 冻结时填（FR3.3）
  frozen_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE eval_tasks (                    -- 评估集任务（tasks.jsonl 行的入库视图）
  id TEXT PRIMARY KEY, evalset_id TEXT NOT NULL REFERENCES evalsets(id),
  task_id TEXT NOT NULL,                     -- tasks.jsonl 行标识（如 fault/bgp-peer-down-01）
  title TEXT NOT NULL, source_ref TEXT NOT NULL,   -- 来源定位（tasks.jsonl:行号）
  content_hash TEXT NOT NULL,                -- 参与 ADR-5 缓存键
  UNIQUE(evalset_id, task_id)
);

-- 实验环境（FR4）
CREATE TABLE labs (
  id TEXT PRIMARY KEY, host TEXT NOT NULL,      -- 承载主机标识（由模块配置派生，FR5.8）
  backend TEXT NOT NULL, env_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('building','ready','faulted','destroyed','error')),
  online INTEGER DEFAULT 1,                  -- HealthMonitor 写入：任一设备 SSH 可达=1（v2.17 H2）
  devices_online TEXT,                       -- "2/3"（在线数/总数，v2.17 H2）
  device_endpoints_json TEXT NOT NULL,       -- (网络名,设备名)→(IP+端口) 适配器映射（ADR-24 权威）；env.ssh_access 基准校验+凭证
  created_at TEXT NOT NULL, destroyed_at TEXT
);
CREATE TABLE lab_snapshots (
  id TEXT PRIMARY KEY, lab_id TEXT NOT NULL REFERENCES labs(id),
  name TEXT NOT NULL, impl_kind TEXT NOT NULL CHECK(impl_kind IN ('native','degraded')),
  backend_ref TEXT,                          -- 原生快照引用 / 降级重放所需引用集
  created_at TEXT NOT NULL
);

-- 优化配置与迭代（FR6.1/ADR-21；v2.4 前移至此——batches/hypotheses 引用本组表，PG 建表顺序要求依赖先建）
CREATE TABLE optimization_profiles (          -- FR6.1 优化配置方案（Schema=配置文档 §3，全字段持久化）
  id TEXT PRIMARY KEY, sut_id TEXT NOT NULL REFERENCES suts(id), name TEXT NOT NULL,
  evalsets_json TEXT NOT NULL,                -- {iterate, holdout, smoke}
  goals_json TEXT NOT NULL,                   -- [{metric, direction, weight}]
  enabled_targets_json TEXT NOT NULL,         -- ["system_prompt","skills",...]
  guards_json TEXT NOT NULL, stop_json TEXT NOT NULL,
  budget_json TEXT NOT NULL, llm_compare_json TEXT,
  created_at TEXT NOT NULL, UNIQUE(sut_id, name)
);
CREATE TABLE iterations (
  id TEXT PRIMARY KEY, sut_id TEXT NOT NULL REFERENCES suts(id),
  profile_id TEXT NOT NULL REFERENCES optimization_profiles(id),
  mode TEXT NOT NULL CHECK(mode IN ('p1','p2')),  -- p1=状态机+人工门（P0）；p2=全自动（P2 开放，此前 409）
  state TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, finished_at TEXT
);

-- 执行（FR5）
CREATE TABLE batches (
  id TEXT PRIMARY KEY, evalset_id TEXT NOT NULL REFERENCES evalsets(id),
  sut_id TEXT NOT NULL REFERENCES suts(id), config_version_id TEXT NOT NULL REFERENCES config_versions(id),
  purpose TEXT NOT NULL CHECK(purpose IN ('baseline_eval','candidate_eval','smoke','iterate_screen',
        'model_compare','noise_calibration')),   -- noise_calibration=噪声带标定批（ADR-17，强制 bypass_cache）
  optimization_profile_id TEXT, status TEXT NOT NULL,
  iteration_id TEXT REFERENCES iterations(id),-- 批次归属优化迭代（P1/P2 模式，ADR-21；可空）
  instrument INTEGER DEFAULT 0,               -- 批次插桩开关（run_task 挂载 overlay）
  permission TEXT NOT NULL DEFAULT 'read-only'
        CHECK(permission IN ('read-only','read-write')),  -- SafetyScanner 判定上下文（ADR-14）
  stats_json TEXT,                           -- 聚合指标缓存
  created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
  eval_task_id TEXT NOT NULL REFERENCES eval_tasks(id),
  lab_id TEXT REFERENCES labs(id), exec_host TEXT, instance_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','env_building','env_ready','sut_running',
        'collecting','judging','done','env_failed','sut_timeout','aborted','cached')),
  cache_key TEXT,                            -- ADR-5；命中则 status='cached' 且 cached_from 指向源 run
  cached_from TEXT REFERENCES runs(id),
  result_summary_json TEXT, exit_code INTEGER,
  started_at TEXT, ended_at TEXT, seq_in_batch INTEGER NOT NULL
);
CREATE TABLE instrumentation_reports (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
  plan_json TEXT NOT NULL, coverage_json TEXT NOT NULL,   -- 覆盖报告（I-8）
  created_at TEXT NOT NULL
);

-- 轨迹索引（FR2.4；正文在文件存储）
CREATE TABLE trace_files (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  ndjson_path TEXT NOT NULL, event_count INTEGER NOT NULL,
  first_ts TEXT, last_ts TEXT, bytes INTEGER NOT NULL
);
CREATE TABLE trace_events_index (            -- 仅高频过滤维度（FR2.5 P1）
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  event_type TEXT NOT NULL, ts TEXT NOT NULL,
  tool_name TEXT, skill_name TEXT, is_error INTEGER DEFAULT 0,
  PRIMARY KEY(run_id, seq)
);

-- 判决（FR7）
CREATE TABLE judgments (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  det_json TEXT, safety_json TEXT, llm_json TEXT,        -- 三层原始输出
  verdict_json TEXT NOT NULL,                 -- 聚合 Verdict（§2.1 Schema）
  judge_prompt_hash TEXT,                     -- FR7.6 冻结审计
  created_at TEXT NOT NULL
);

-- 优化（FR6）
CREATE TABLE hypotheses (
  id TEXT PRIMARY KEY, sut_id TEXT NOT NULL REFERENCES suts(id),
  iteration_id TEXT REFERENCES iterations(id),-- 归属迭代（人工门②提交时关联，v2.3）
  hypothesis_json TEXT NOT NULL,              -- FR6.3 Schema（含 predicted_affected_tasks）
  status TEXT NOT NULL CHECK(status IN ('draft','proposed','critic_passed','applied',
        'accepted','rejected','withdrawn')),
  critic_json TEXT,                           -- 预审记录
  predicted_hit_rate REAL,                    -- 决策阶段回算
  failure_reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE experiments (                    -- FR8.4 实验记录
  id TEXT PRIMARY KEY, sut_id TEXT NOT NULL REFERENCES suts(id),
  hypothesis_id TEXT REFERENCES hypotheses(id),
  baseline_version_id TEXT NOT NULL, candidate_version_id TEXT NOT NULL,
  baseline_batch_id TEXT REFERENCES batches(id), candidate_batch_id TEXT REFERENCES batches(id),
  decision TEXT CHECK(decision IN ('promoted','reverted','pending','advise_manual')),
  decision_reason TEXT, evidence_json TEXT,
  created_at TEXT NOT NULL
);

-- 迭代决策（FR9，v1.5 增）
CREATE TABLE noise_bands (            -- 噪声带标定（显式标定批次产出，ADR-17）
  id TEXT PRIMARY KEY,
  evalset_id TEXT NOT NULL REFERENCES evalsets(id),
  evalset_hash TEXT NOT NULL,         -- 冻结版本内容哈希（变更即失效需重标）
  metric TEXT NOT NULL, sigma REAL NOT NULL, delta REAL NOT NULL,
  k INTEGER NOT NULL, low_confidence INTEGER DEFAULT 0, calibrated_at TEXT NOT NULL,
  UNIQUE(evalset_id, evalset_hash, metric)
);

-- 平台 LLM 记账（预算熔断数据源 FR5.6）
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  component TEXT NOT NULL,                    -- attribution/hypothesis/critic/judge/sut/<...>
  model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_ms INTEGER,
  run_id TEXT, batch_id TEXT
);

-- 断点续跑（FR5.5）
CREATE TABLE checkpoints (
  scope TEXT PRIMARY KEY,                     -- batch_id | iteration_id
  state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
```

索引：`runs(batch_id)`、`runs(cache_key)`、`trace_events_index(event_type, run_id)`、`llm_calls(batch_id)`、`audit_log(ts)`。

---

## 3. I-1：HostService（SUT 承载适配层服务端）

```python
class HostService:
    async def create_instance(self, image_ref: str, config_version_id: str,
                              lab: LabHandle,
                              permission: Literal["read-only","read-write"],
                              instrument: bool,
                              host: HostRef = HostRef.LOCAL,
                              cred: ResolvedCreds | None = None) -> InstanceHandle
        # cred：由 env.ssh_access 解析（password 明文或 password_ref→CredentialStore），
        # 注入 SUT 容器 env；平台侧编排同源使用
        # host：carrier.hosts 主机池键（FR5.8 模块自管）；P0 默认 main=本机 Docker Desktop/npipe；
        # 远端主机经 SSH 隧道 DOCKER_HOST 接入（P1 能力）
        """1. GitStore.export(config_version_id) → 配置目录
           2. instrument=true → instrumentor.build_overlay(config_dir)
           3. docker create：挂载 /agent-config=配置(或 overlay)目录、/trace 卷；
              网络=[lab.network, trace_net]（不接 mgmt，FR1.6）
           4. 凭证注入 env（不写文件）"""
    async def start/stop/destroy(...)
    async def submit_task(self, instance, task: TaskSpec) -> RunHandle    # 适配器分发
    async def collect(self, handle, timeout_s: int = 1800) -> TaskResult  # NFR4 默认 30min
```

配置注入/凭证/网络隔离约定见需求文档 3.2"关键约定"。

---

## 4. I-3：SandboxService 与后端抽象

```python
class SandboxService:            # I-3 服务端实现，转发到后端适配器
    def __init__(self, backends: dict[str, SandboxBackend]): ...
    async def build_lab(self, spec: EnvSpec, backend: str | None = None) -> LabHandle
        # backend 缺省 = 按 spec.backend 同名解析 sandbox.backends 配置键；多实例键 <type>-<n> P0 即用（ADR-24）
        # 端点解析三级（ADR-24）：① 适配器返回 (网络名,设备名)→(IP+端口) 映射（权威——
        #   ENSP host-agent 节点映射 / PNETLab 拓扑读取），写 labs.device_endpoints_json；
        # ② env.ssh_access 基准校验（单实例=一致；多实例=实例专属端点）+ 凭证载体；③ 站点覆盖先行套用
        # （配置键与 env.yaml backend 枚举同名：ensp-pro/pnetlab/containerlab，FR5.8）；
        # 显式传入仅用于 P2 多实例选择（模块内自动资源选择）
        # 设备端点 = spec.ssh_access 声明；构建后验证可达（不一致→环境错误，FR4.3）
    async def snapshot/restore/reset/destroy(...)
    async def prepare(self, lab, actions: list[PrepareAction]) -> None
        # 任务级评测准备（FR3.1，ADR-20 两种手段，多设备多命令）：
        #   exec_cli → 先备份涉及设备配置（backups/<task_id>/<device>.cfg）再经 sshpool 逐设备逐条下发；
        #   call_api → 解析 env.config_apis[api] 端点，组装请求（组网名/场景名/devices）调用外部 API；
        # 全部操作写 ops.jsonl；任一失败即抛出（调用方按手段恢复后重试 ≤2）
    async def restore_task(self, lab, applied: list[AppliedAction]) -> None
        # 任务后恢复（判决之后调用，ADR-20 按手段匹配，L1 快路径）：
        #   exec_cli → 按厂商模板"清空当前配置→重放备份全量"经 CLI 恢复；
        #   call_api → 以记录原参数 + 撤销标记（undo）重调外部 API（集成契约）；
        # 恢复后 ready_checks 快检；失败重试 ≤2 → 仍失败转 L2 环境级恢复（snapshot/重放）
    # during_task 任务中动态执行（P1）：直接接受 exec_cli 动作，契约 P1 定义（v2.0 无故障编译层）
    async def exec_readonly(self, lab, device, cli) -> str      # 平台侧校验通道（判定 DSL 用）
    async def wait_ready(self, lab, timeout_s=600) -> ReadyReport

class SandboxBackend(ABC):       # 后端适配器（ENSPPro/PNETLab/containerlab/Fake）
    async def build(self, spec) -> LabHandle
    async def snapshot(self, lab) -> SnapRef          # native 或 degraded
    ...
```

---

## 5. I-4：TraceCollector / TraceStore / TraceDiffer

```python
class TraceCollector:            # 独立进程（主设计 3.5，v1.6 增）
    async def serve_otel(self)                    # OTLP receiver（grpc:4317/http:4318）；
                                                   # span→TraceEvent 映射 + rsi.run_id 归属提取
    async def tail_ndjson(self, run_id, path)     # events.ndjson / wiretap/*.ndjson 尾随（断点续读）
    async def ingest(self, events: list[TraceEvent])
        # 统一管道：redact(P1) → SeqAllocator.next(run_id) → 落盘+索引+WS fan-out
    async def finalize(self, run_id)
        # NDJSON EOF（文件大小两次采样不变）+ OTel 宽限(默认 5s 可配)
        # → 写 meta.json → 更新 trace_files → 关闭该 run 的 WS fan-out；迟到事件标 late 照常入库

class TraceStore:                # I-4 实现
    async def query(self, f: TraceFilter) -> list[TraceEventRef]
    async def cluster_failures(self, f: TraceFilter, group_by: FailureFeature) -> Clusters  # P1
    async def stats(self, f: TraceFilter) -> TraceStats

class TraceDiffer:               # FR10.8 版本间轨迹对比（P1）
    def diff(self, run_a: RunId, run_b: RunId) -> TraceDiff
        """两 run（同任务、不同 SUT 版本，按命名规范天然配对）事件级对齐：
           以归一化步骤序（LLM 轮次 × tool_call 名称）对齐，输出——
           新增/消失的工具调用、命令序列 diff、LLM 请求 system/messages 差异摘要、
           逐步时延与 token 对比；供前端并排渲染与人工归因"""
```

---

## 6. I-5：JudgeService

```python
class JudgeService:              # I-5 服务端；三层流水线 + 聚合
    async def judge_run(self, run_id) -> Verdict
    async def judge_batch(self, batch_id) -> list[Verdict]
    async def pairwise(self, baseline_run, candidate_run) -> PairwiseReport   # P1
```

三层内部组件（DeterministicJudge/SafetyScanner/LLMJudge/AntiHackDetector/VerdictAggregator）为 judge 模块内部设计，见主设计文档 3.6。

---

## 7. I-6：GitStore（版本管理服务端）

```python
class GitStore:                  # 封装 GitPython，全部操作记 audit_log
    def export(self, version_id: VersionId, dest: Path) -> None        # 供 overlay/实例挂载
    def snapshot(self, sut_id) -> VersionId                            # FR6.5
    def commit_candidate(self, base: VersionId, cs: Changeset, hyp_id) -> VersionId
    def promote(self, candidate: VersionId) -> None                    # tag fast-forward
    def rollback(self, sut_id, version_id: VersionId) -> None
    def diff(self, a, b) -> FileDiff; def lineage(self, sut_id) -> LineageGraph
    def freeze_evalset(self, evalset_id) -> FreezeRecord                # FR3.3
    def verify_freeze(self) -> bool                                     # 启动自检（FR7.6）
```

---

## 8. I-8：插桩外部契约（容器内代理与 SDK）

**pg-mcp-proxy**（MCP stdio 代理，配置级注入核心，P0）：

```
mcp.json 中 "command": "npx my-netops-mcp" → "command": "pg-mcp-proxy",
                                          "args": ["--", "npx", "my-netops-mcp"]
```

行为契约：标准 MCP stdio 双向代理（JSON-RPC 透传）；对 `tools/call` 请求/响应旁路记录 `tool_call`/`tool_result` 事件（含耗时、异常）到 `/trace/events.ndjson`；自身无状态；崩溃时透传断开行为与原进程一致（**不改变 SUT 业务语义**）。

**pg_trace SDK**（`instrumentor/sdk/pg_trace.py`）：

```python
def pg_trace(event_type: str, data: dict, duration_ms: int | None = None) -> None
    # 单文件实现：追加写 /trace/events.ndjson（TraceEvent 结构，见 §2）
    # skill 包装模板与脚本 import 桩复用；统一 PG-TRACE 标记、可幂等移除
```

**事件文件契约**：`/trace/events.ndjson` 每行一个 TraceEvent（§2 Schema）；由节点/主节点 collector 汇聚统一 seq 后入库（NFR2⑤）。**wiretap 原文契约（v1.9）**：`/trace/wiretap/<run_id>.ndjson` 保留载体原生帧——P0=OpenCode prompt-logger 的 assembled/http 原帧（`file` 选项配置写入）；P2=Claude Code stream-json 原帧逐行。适配器负责写入；collector 解析为 TraceEvent 后**原帧永久保留**（审计原文；unredacted——FR2.7 脱敏仅作用于解析产物，原文按需人工脱敏后查阅）。

---

## 9. I-9：Platform API（REST + SSE/WS，FastAPI，`api/routers/`，前缀 `/api/v1`，ADR-18）

### 9.0 统一约定

- **认证**：除 `POST /auth/login` 与 `GET /system/health` 外全部端点要求 `Authorization: Bearer <JWT>`；RBAC 按 6.1 矩阵（下表"角色"列：A=admin、E=agent_engineer、R=researcher、P=approver）。
- **分页**：游标分页——请求 `?cursor=&limit=`，响应头 `x-pagination: {next_cursor, has_more}`。
- **错误体**：`{code, message, detail}`；错误码表：

| code | HTTP | 场景 |
|------|------|------|
| UNAUTHORIZED | 401 | 未携带/无效 token |
| FORBIDDEN | 403 | RBAC 拒绝 |
| NOT_FOUND | 404 | 对象不存在 |
| VALIDATION | 422 | 请求体校验失败 |
| CONFLICT | 409 | 冻结后修改 / 非法状态转换 / 非 fast-forward 晋升 / 唯一名冲突 |
| BUDGET_EXCEEDED | 429 | 预算熔断中 |

- **审计**：全部写操作自动进 audit_log（@audited，设计 3.11.3）。
- **契约源（ADR-18）**：FastAPI Pydantic 模型 → /openapi.json → openapi-typescript 生成前端客户端；本表人工维护供评审。

### 9.1–9.17 端点契约（`请求 → 响应 | 角色 | 错误`）

| # | 端点 | 契约 |
|---|------|------|
| 9.1 | `POST /auth/login` | `{username, password} → {token, expires_at}` \| ∅公开 \| 401 |
| | `GET /auth/me` | `→ {id, username, role}` \| A/E/R/P \| 401 |
| 9.2 | `GET /suts` | `?cursor → 分页[{id,name,carrier,image_ref,baseline_version_id,description}]` \| A/E/R |
| | `POST /suts` | `{name, carrier, image_ref, description} → 201{id}` \| A/E \| 422/409 名冲突 |
| | `POST /suts/{id}/verify-image` | `→ {checks: [{item, passed, detail}]}`（G5：docker inspect+run --version 检查载体 CLI//trace//插件预装——逐项结果，"验证镜像"按钮数据源） \| A/E |
| | `GET /suts/{id}` | `→ Sut + versions[{id,label,kind,created_at}]` \| A/E/R \| 404 |
| | `PUT/DELETE /suts/{id}` | `→ 204` \| A/E \| 404 |
| | `POST /suts/{id}/versions:promote` | `{candidate_version_id, baseline_batch_id?, candidate_batch_id?, reason?} → 204`（人工模式带双批引用与理由时**自动登记 experiments**，证据链与迭代模式同构，ADR-21） \| A/E \| 409 非 fast-forward |
| | `POST /suts/{id}/versions:rollback` | `{version_id} → 204` \| A/E \| 404 |
| | `GET /suts/{id}/lineage` | `→ {nodes:[{version_id,label,kind,parent_ids,experiment_ids}], edges}` \| A/E/R |
| | `POST /suts/{id}/versions:snapshot` | `→ 201{version_id}`（手动快照，FR6.5 同机制） \| A/E |
| | `GET /suts/{id}/files` | `?path=&version= → 目录树/文件内容`（version 可选=浏览历史版本；**P0 人工模式核心入口**） \| A/E/R |
| | `PUT /suts/{id}/files/{path}` | `{content, commit?=false, message?} → 204`（写 staging worktree；commit=true 经 TargetRegistry 校验后提交新版本——**人工修改同在白名单执行层校验内**） \| A/E \| 404/409 越界路径 |
| | `POST /suts/{id}/files`、`DELETE /suts/{id}/files/{path}` | `新建/上传 / 删除（staging）→ 201/204` \| A/E \| 409 越界路径 |
| 9.3 | `GET /versions/{a}/diff/{b}` | `→ [{path, op, patch}]`（Monaco 渲染） \| A/E/R |
| 9.4 | `GET /evalsets` | `?cursor → 分页[{id,name,purpose,frozen_at?,content_hash?}]` \| A/E/R |
| | `POST /evalsets` | `{name, purpose, env_yaml, tasks_jsonl, configs{path:content}, site_overrides?{site: env_yaml}} → 201{id}`（站点覆盖文件随建随传，FR3.1） \| A \| 409 名冲突 |
| | `GET /evalsets/{id}` | `→ {…, env: EnvSpec, tasks:[TaskRecord 摘要], topology}` \| A/E/R \| 404 |
| | `PUT /evalsets/{id}` | `→ 204` \| A \| **409 已冻结** |
| | `POST /evalsets/{id}:freeze` | `→ {git_ref, content_hash, frozen_at}` \| A \| 409 重复冻结 |
| | `GET /evalsets/{id}/versions` | `→ [{v, git_ref, content_hash, frozen_at}]`（冻结版本列表） \| A/E/R |
| | `POST /evalsets/{id}:assign` | `{purpose ∈ iterate\|holdout\|generalization\|smoke} → 204` \| A |
| | `POST /evalsets/{id}:export` | `→ 三件套原格式 zip（FR3.5）` \| A/E/R |
| | `POST /evalsets:import` | `multipart zip → 201{id}` \| A \| 409 名冲突 |
| | `POST /evalsets:generate` | `{template, params} → 201{id}`（P2） \| A |
| 9.5 | `GET /profiles?sut_id=` | `→ 分页[OptimizationProfile]` \| A/E/R |
| | `POST /profiles` | `OptimizationProfile → 201{id}` \| A/E \| 409 同 SUT 名冲突 |
| | `GET/PUT/DELETE /profiles/{id}` | `→ Profile / 204` \| A/E/R·A/E |
| 9.6 | `POST /batches` | `{evalset_id, sut_id, config_version_id?, purpose ∈ baseline_eval\|candidate_eval\|smoke\|iterate_screen\|model_compare\|noise_calibration, profile_id?, instrument=false, permission="read-only"} → 201{batch_id}` \| A/E/S \| 404/422（holdout 判定要求已冻结→409） |
| | `GET /batches/{id}` | `→ {batch:{…,status,stats}, progress:{total,done,failed,cached}}` \| A/E/R |
| | `GET /batches?sut_id=&status=` | `→ 分页[Batch]` \| A/E/R |
| | `POST /batches/{id}:pause \| :resume` | `→ 204` \| A/E \| 409 非法状态 |
| | `POST /batches/{id}:abort` | `→ 204` \| A/E \| 409 |
| | `POST /batches/{id}:rerun` | `{scope: failed\|all, bypass_cache=true} → 204`（重测 FR9.1；rerun 批次**复制原批次 purpose**，以 `params.rerun_of`（原批 id）与 `params.rerun_scope`（failed\|all）标记重测来源与范围——purpose 枚举无 rerun 值，D-9） \| A/E \| 409 |
| | `GET /batches/{id}/runs` | `?cursor → 分页[Run 摘要]` \| A/E/R |
| 9.7 | `GET /runs/{id}` | `→ {id,batch_id,task_id,status,exit_code,result_summary,judgment_id?,started_at,ended_at}` \| A/E/R |
| | `GET /runs/{id}/traces` | `?cursor&event_type= → 分页[TraceEvent]` \| A/E/R |
| | `POST /runs/{id}:re-judge` | `{judge_overrides?} → {judgment_id}`（重判 FR9.1） \| A/E \| 409 |
| 9.8 | `GET /streams/events` | SSE（见 9.20） \| A/E/R/P |
| | `GET /streams/runs/{id}` | WS（见 9.20） \| A/E/R |
| | `GET /traces/diff` | `?run_a=&run_b= → TraceDiff`（FR10.8） \| A/E/R |
| | `POST /traces:query` | `{filter: run_ids?/task_id?/event_type?/is_error?/from?/to?} → 分页[事件引用]`（跨 run 检索，FR2.5，P1） \| A/E/R |
| | `POST /traces:stats` | `{filter} → {tokens/steps/duration 分布}`（FR2.5，P1） \| A/E/R |
| 9.9 | `GET /runs/{id}/judgment` | `→ Verdict` \| A/E/R |
| | `POST /judgments:pairwise` | `{baseline_run, candidate_run} → PairwiseReport`（P1） \| A/E/R |
| 9.10 | `GET /hypotheses?sut_id=&status=` | `→ 分页[Hypothesis+status]` \| A/E/R |
| | `POST /hypotheses` | `Hypothesis → 201{id}`（draft；P1 人工导入通道） \| A/E |
| | `GET/PUT /hypotheses/{id}` | `→ Hypothesis / 204`（PUT=P1 人工修订） \| A/E/R·A/E |
| | `POST /hypotheses/{id}:submit-review` | `→ 204`（P2 进入 Critic） \| A/E \| 409 非法状态 |
| 9.11 | `GET /experiments?sut_id=` | `→ 分页[ExperimentRecord]`（证据链） \| A/E/R/P |
| | `GET /experiments/{id}` | `→ ExperimentRecord{decision, decision_reason, evidence, stale}` \| A/E/R |
| | `GET /experiments/{id}/compare` | `→ {files_diff, task_table:[{task_id, base:{score,verdict_ref}, cand:{…}, delta, change_flag}], trace_diff_refs:[{task_id,run_a,run_b}]}`（FR10.8） \| A/E/R |
| | `GET /experiments/{id}/sft-report` | `→ text/markdown`（FR6.9 SFT 评估报告查看） \| A/E/R |
| 9.12 | `POST /iterations` | `{sut_id, profile_id, mode ∈ p1\|p2} → 201{iteration_id}`（FR10.2，**P0 交付**，ADR-21：p1=状态机+人工门；p2 于 P2 阶段开放，此前 409；P0 人工模式不经本端点。**v2.14 迭代互斥**：同一 sut_id 存在活跃迭代→409） \| A/E \| 404/**409 进行中/同 SUT 互斥** |
| | `GET /iterations/{id}` | `→ {id, sut_id, mode, state, current_step, round, experiments[], budget_used{tokens, cost_usd, wall_time_s}}` \| A/E/R |
| | `POST /iterations/{id}:pause \| :resume \| :abort` | `→ 204`（暂停/恢复/终止；非法状态 409） \| A/E \| 409 |
| | `DELETE /iterations/{id}` | `→ 204`（**软删除**=archived 态，证据链 keep-all 不物理删；进行中 409 先 abort） \| A/E \| 409 |
| | `POST /iterations/{id}/confirm-direction` | `{selected: [cluster_id]} → 204`（**人工门①**：优化方向确认，P1 模式） \| A/E \| 409 非等待态 |
| | `POST /iterations/{id}/hypothesis` | `{Hypothesis} → 201{hypothesis_id}`（**人工门②**：假设提交，写 hypotheses.iteration_id 并推进状态机） \| A/E \| 422/409 |
| | `POST /iterations/{id}/review` | `{verdict: pass\|reject, reasons[]} → 204`（**人工门③**：预审裁决——页面展示 DeterministicCritic 结果 + diff） \| A/E \| 409 |
| | `POST /iterations/{id}/decision` | `{action: promote\|revert\|advise_hold, reason} → 204`（**人工门④**：决策执行——引擎调 versioning 落地并回算命中率） \| A/E \| 409 |
| 9.13 | `GET /labs` | `→ [{id, host, backend, status, created_at}]` \| A/E/R |
| | `POST /labs` | `{evalset_id? \| env_yaml} → 201{lab_id}`（手动建环境：调试/独立使用） \| A/E \| 422 |
| | `GET /labs/{id}` | `→ Lab 详情` \| A/E/R |
| | `DELETE /labs/{id}`、`POST /labs/{id}/reset` | `→ 204`（销毁 / 按原 spec 重置） \| A/E \| 409 |
| | `POST /labs/{id}/snapshots`、`POST /labs/{id}/restore` | `{name} → 201{snapshot_id} / {snapshot_id} → 204`（手动快照/恢复） \| A/E |
| | `GET /labs/{id}/topology` | `→ {nodes:[{name,type,status,mgmt_ip}], links:[{a,b,type}]}`（FR10.3） \| A/E/R |
| | `GET /labs/{id}/devices/{dev}/readiness` | `→ ReadyReport` \| A/E/R |
| | `POST /labs/{id}/devices/{dev}/exec` | `{cli} → 输出`（只读诊断，前缀白名单约束） \| A/E/R \| 409 非白名单 |
| | `GET /sandbox/backends` | `→ [{key, type, host, reachable, readiness, subnets}]`（各仿真后端可达性/就绪探测/网段池状态，FR5.8） \| A/E/R |
| 9.14 | `GET /instances` | `?status=&host= → [{id, sut_id, run_id, host, status, created_at}]`（运行 SUT 容器实例，I-9 状态查询/FR10.3） \| A/E/R |
| | `DELETE /instances/{id}` | `→ 204`（清理残留实例） \| A/E |
| 9.15 | `GET /batches/{id}/coverage` | `→ CoverageReport{covered:[{aspect,method}], not_instrumentable:[{aspect,reason}]}` \| A/E/R |
| 9.16 | `GET /approvals` | `→ [Approval{hypothesis_id, diff, risk}]`（P2） \| A/P |
| | `POST /approvals/{id}:approve \| :reject` | `{reason} → 204`（P2） \| P |
| 9.17 | `GET /dashboard/metrics` | `?metric=&sut_id=&from=&to= → {series:[{ts,value}]}`（P1 全量） \| A/E/R |
| | `GET /dashboard/model-compare` | `?sut_id=&evalset_id= → {models:[{model, metrics{}}]}`（P1） \| A/E/R |
| | `GET /dashboard/defect-dist` | `?sut_id= → {A:n, B:n}`（P1） \| A/E/R |
| | `GET /dashboard/noise-bands` | `?evalset_id= → [{metric, sigma, delta, k, evalset_hash, low_confidence, calibrated_at, valid}]`（噪声带标定状态，ADR-17，P1） \| A/E/R |
| 9.18 | `GET /audit-logs` | `?actor=&action=&object_type=&from=&to= → 分页[审计记录]`（NFR1④ 审计查询） \| A |
| 9.19 | `POST /credentials` | `{name, kind ∈ device_ssh\|nms_api\|llm_api\|backend_ssh, secret} → 201{id}`（值永不回显） \| A |
| | `GET /credentials` | `→ [{id,name,kind,created_at}]`（无 secret） \| A |
| | `POST /credentials/{id}:rotate` | `{secret} → 204` \| A |
| | `GET/POST /users`、`PUT /users/{id}` | `→ [User] / 201 / 204（disabled、role 变更）` \| A |
| | `GET /system/settings` | `→ [{path, value, source: env\|yaml\|default, mutable, 需求号}]`（平台参数全量，动态/静态标注） \| A |
| | `PUT /system/settings/{path}` | `{value} → 204`（**动态项**：写回 settings.yaml + 热生效 + 审计；**静态项** 409 指引改文件重启。动态范围：sandbox/carrier 超时与重试、judge 参数、optimizer 门槛、decision.noise/alpha、orchestrator.budget、instrumentor.policy、tracing.redact/archive、llm.providers/defaults） \| A \| 409 静态项 |
| | `POST /system:stop`、`DELETE /system:stop` | `→ 204`（置 STOP 熔断 / **清除熔断恢复运行**，FR5.5） \| A |
| | `GET /system/freeze` | `→ {verified, manifest_hash, items[], last_checked, target_paths, frozen_paths}`（冻结校验状态+**TargetRegistry 路径域清单**——安全边界只读页数据源，G3） \| A/E/R |
| | `GET /system/logs` | `?run_id=&experiment_id=&level=&from=&to= → 分页[日志行]`（平台运行日志检索，NFR5 排障主路径） \| A/E |
| | `GET /system/health` | `→ {status, version, components:{db, git, collector, backends[], freeze}}`（组件级健康） \| ∅公开 |
| | `GET /system/dependencies` | `→ [{key, kind, status: up\|down\|degraded, detail, latency_ms, last_checked}]`（**H1-H8 v2.17 全外部依赖状态**——8 类检查器实时结果；kind ∈ docker\|lab\|llm\|git\|db\|collector\|config_api\|nms；HealthMonitor 30s 轮询） \| A/E/R |

### 9.20 SSE / WebSocket 契约

- **SSE `/api/v1/streams/events`**（Bearer header 鉴权）：单通道全量状态推送（EventBus 发布-订阅，asyncio queue）。事件信封：`{event, ts, data: {object_id, from, to}}`；事件枚举：`batch.status_changed | run.status_changed | lab.status_changed | iteration.status_changed | instance.status_changed`。
- **WS `/api/v1/streams/runs/{id}?token=`**（短时效 query token）：首帧 `{type:"tail", events:[≤N 条]}` → 后续 `{type:"append", event: TraceEvent}`；30s 心跳 ping/pong。

---

## 10. I-10：载体适配器（CarrierAdapter 插件接口）

```python
class CarrierAdapter(ABC):
    async def build_image(self, sut_def: SutDefinition) -> str
    async def start(self, instance, config_mount: Path, env: dict) -> None
    async def submit_task(self, instance, task: TaskSpec) -> RunHandle
    async def collect_result(self, handle) -> TaskResult        # 结构化输出约定见 Q6 与本节
    def trace_channels(self, instance) -> TraceChannels         # otel_endpoint | ndjson_paths
```

**TaskResult 结构化输出约定**（Q6 落地）：适配器从 SUT stdout 提取最后一个 JSON 代码块（`task_complete/root_cause/actions[]/confidence`，Schema 见 §2），提取失败视为格式缺陷（进归因失败模式"输出不可解析"，A 类缺陷样本）。

各实现（**P0=OpenCodeAdapter（优先载体）/Fake；P2=ClaudeCodeAdapter**，ADR-22）差异表见主设计文档 3.2.2 与附 C。

---

## 11. 编排服务接口（orchestrator 对 API 层与子系统暴露）

```python
class BatchRunner:
    def __init__(self, sandbox: SandboxService, carrier: HostService,
                 instrumentor: Instrumentor, collector: TraceCollector,
                 judge: JudgeService, ckpt: CheckpointStore, budget: BudgetController): ...
    # Instrumentor = 插桩五阶段门面（主设计 3.3），供编排层整体调用
    # collector = TraceCollector（主设计 3.5）：finalize/flush 通道；
    #             TraceStore 查询接口另行注入归因/对比等使用方

    async def run_batch(self, batch_id: BatchId) -> BatchStats:
        """批次主循环：逐任务执行 run_task()；异常不中断整批（FR5.5）。"""

    async def run_task(self, batch_id: BatchId, task: EvalTask, seq: int) -> RunResult:
        """单任务流水线，状态落 runs 表：
        1. cache 查询（ADR-5 键）→ 命中则 status='cached' 直接复用判决（不执行不弄脏环境）
        2.（数据集级，仅批次首个任务）选后端实例 → build_lab(env) 一次 → wait_ready → snapshot(snap0)
           构建失败→重建≤2→仍失败整批 failed；snap0 为 L2 兜底基线（ADR-20）
        3.（任务级）prepare 评测准备（ADR-20 两种手段，多设备多命令，设备名引用；
           exec_cli 先备份涉及设备配置；失败→按手段恢复重试≤2→仍失败 'env_failed'（0 分）+ L2 恢复）
        4. carrier.create_instance(...)  含 overlay（若批配置 instrument=true）
        5. carrier.submit_task(task_spec) 超时→abort→'sut_timeout'（缺试计 0 分，FR7.4）
        6. collector.finalize(run_id)    等待采集器 flush（含脱敏管道）
        7. judge.judge_run(run_id)       → Verdict 落库（终态断言需故障态在场，故恢复在判决后）
        8. sandbox.restore_task(lab, applied)  任务后恢复（ADR-20：CLI 配置恢复 / API 撤销；
           失败重试≤2→L2 环境级恢复；恢复失败且 L2 仍失败→该任务后环境异常标记）
        9. checkpoint.save(run_id, state)（FR5.5 断点续跑：每步完成即持久化，
           恢复时从 checkpoint 反查已完成的 seq_in_batch 跳过）
        """

class IterationEngine:
    async def iterate_once(self, sut_id: SutId, profile: OptimizationProfile,
                           mode: Literal["p1", "p2"] = "p1") -> ExperimentRecord:
        """一轮迭代（对应需求 2.3 八步中的 5–8 步；ADR-21：mode=p1=P0 交付的
           状态机+人工门执行器；mode=p2=P2 全自动，P2 阶段开放）：
        ATTRIBUTING    ← optimizer.attribution.analyze(failed_runs)  [LLM 点1]
        HYPOTHESIZING  ← optimizer.hypothesis.generate(...)           [LLM 点2]
        REVIEWING      ← optimizer.critic.review(diff)                [LLM 点3]
                        拒绝→修复循环≤5→仍拒→人工队列（FR6.10）
        APPLYING       ← versioning.snapshot(强制) → optimizer.applier.apply()
        VERIFYING      ← smoke 冒烟集（FR3.7）→ 迭代集小子集预筛（FR9.1 分级）
                        → 达阈值才 hold-out 全量（ADR-5 缓存生效）
        DECIDING       ← decision.selector.decide(baseline_stats, candidate_stats)
                        → promote | revert | advise_manual（FR9.3）
                        → 回算 predicted_hit_rate 写回 hypotheses
        任何一步预算超限 → budget.halt() 熔断（FR5.6）
        连续 N 轮无收益 → exploration.switch_mode() 或停机（FR9.4/9.6）
        """

class BudgetController:
    def check_and_reserve(self, scope: BatchId|IterationId, est: CostEstimate) -> None  # 超限 raise BudgetExceeded
    def record(self, scope, actual: LLMUsage|WallTime) -> None     # 消费 llm_calls 表实时累加
    def halt(self, scope) -> None                                  # 置 STOP 标记，BatchRunner 轮询

class CheckpointStore:             # SQLite 表 checkpoints(scope, state_json, updated_at)
    def save(self, scope, state: dict) -> None; def resume(self, scope) -> dict | None
```

批次状态机与重试矩阵见主设计文档 3.1。

---

## 12. 优化与决策服务接口（orchestrator 调用）

```python
# ── optimizer（FR6）──
class AttributionAnalyzer:       # FR6.2 [LLM 点1] prompts/attribution/cluster.v1
    async def analyze(self, failed_runs, traces, coverage,
                      sut_config: ConfigSnapshot,
                      opt_history: list[dict]) -> AttributionReport
        # sut_config（v2.2）：产生轨迹的 SUT 配置版本原文（run→config_version 经 GitStore.export）；
        # opt_history（v2.6）：该 SUT 近期实验摘要（读 experiments+hypotheses，
        #   最近 N=10 轮：{round, proposal_type, decision, failure_mode, reason}）——
        #   归因时标注"此簇已尝试 X 未果"，Top-N 排序排除无效方向
        # token 控制——轨迹定现象后仅取 suspected_elements 相关文件原文进 LLM
        """预聚类（规则）：按 failure_type 特征（工具序列缺失命令模式/报错聚类/根因比对失败）粗分，
           并对照 SUT 原文：漏查步骤在提示词/skill 已写→执行缺陷、未写→指引缺失；
           幻觉命令在知识库已有→执行问题、没有→知识缺失；
           LLM 精分类：每簇样本轨迹摘要 + 相关要素原文 → 失败模式标签 + A/B 缺陷标注
           （FR6.9 分类框架；B 类"已提供"以原文核对为准）
           输出：[{failure_mode, defect_class, cluster_size, sample_run_ids,
                  evidence（轨迹引用 + SUT 原文证据引用：文件与位置）}] 排序"""

class HypothesisGenerator:       # FR6.3 [LLM 点2] 两阶段（DGM 问题单模式）
    async def stage1_problem_statement(self, cluster) -> ProblemStatement   # prompts/hypothesis/diagnose.v1
    async def stage2_propose(self, ps, profile: OptimizationProfile,
                             failed_hyp_hashes: list[str]) -> Hypothesis    # prompts/hypothesis/propose.v1
        """输入含：相关要素原文（suspected_elements 定位文件全文，自 GitStore——写 diff 基于原文而非摘要）、
           白名单内可改要素清单（仅 enabled_targets）、失败假设摘要（防重复 FR6.8）；
           switch_model 类型时 diff_content 为空、model_config_patch 填充；
           输出 JSON schema 严格校验，retry≤2"""

class Critic:                    # FR6.10 [LLM 点3] prompts/critic/review.v1（P2 自动；P1 人工表单）
    async def review(self, changeset: Changeset, profile) -> CriticVerdict
        """确定性检查（applier.targets 同源规则）：
           路径越界 / 触碰冻结域 / 超 enabled_targets / 声明与实际 diff 不符；
           LLM 六类审查（FR6.10 清单）→ {verdict: pass|reject|repair, reasons}
           reject→修复循环：携 reasons 回 HypothesisGenerator 修复，≤5 轮"""

class ChangeApplier:             # FR6.4/6.5
    async def apply(self, sut_id, hyp: Hypothesis) -> Changeset
        """强制前置：versioning.snapshot()（FR6.5，未快照直接 raise）；
           白名单执行层校验（targets.regexp 域匹配 + 冻结域排除）→ 双重校验后写工作区
           → versioning.commit_candidate(changeset) → 返回 candidate_version_id"""

class SftAssessor:               # FR6.9
    async def assess(self, sut_id, defect_b_clusters, opt_history) -> SftReport | None
        """准入条件程序化检查：同缺陷类连续 N 轮无收益（读 experiments）+样本量门槛+
           知识/skill 已尝试标记；满足→LLM 起草报告（含样本清单）；P0=模板人工填写"""

class TargetRegistry:            # 优化项→路径域映射（FR6.1 执行层校验的规则源）
    TARGET_PATHS = {
      "system_prompt": r"suts/{id}/system-prompt(\.md)?",
      "skills":       r"suts/{id}/skills/.*",
      "scripts":      r"suts/{id}/scripts/.*",
      "mcp_config":   r"suts/{id}/mcp\.json",
      "mcp_src":      r"suts/{id}/mcp-src/.*",
      "memory":       r"suts/{id}/memory/.*",
      "knowledge":    r"suts/{id}/knowledge/.*",
      "model_config": r"suts/{id}/model\.json",
    }   # 冻结域=此表之外的一切路径：judge/**、prompts/**、冻结后的 evalsets/**
        # （期望答案内嵌 tasks.jsonl，按整目录冻结保护）

# 执行器协议（ADR-16：P1 人工 / P2 自动 同一状态机，仅换实现）
class HypothesisSource(Protocol):            # HYPOTHESIZING 状态执行器
    def generate(self, ps: ProblemStatement, profile: OptimizationProfile,
                 failed_hyp_hashes: list[str]) -> Hypothesis
    # P1=WebformHypothesisSource（表单/文件导入）；P2=LLMHypothesisSource（两阶段 LLM）
class ReviewGate(Protocol):                  # REVIEWING 状态执行器
    def review(self, changeset: Changeset, det_verdict: CriticVerdict) -> CriticVerdict
    # P1=ManualReviewGate（人工表单）；P2=AutoReviewGate（LLMCritic）

# ── decision（FR9）──
class NoiseBandCalibrator:       # FR9.2（ADR-17 显式标定批次）
    def calibrate(self, sut_id, evalset_id, k: int = 3) -> list[NoiseBand]
        """编排 batches.purpose='noise_calibration'（强制 bypass_cache）：基线版本在
           分层抽样任务子集（按域分层，min(10, 任务总数)）重复 k 次 → 逐指标 σ、δ=factor×σ
           → 写 noise_bands 表（绑定 evalset_id+evalset_hash+metric，冻结版本不变即有效）；
           有效任务数 < 抽样数一半 → low_confidence 标记 + 告警；
           未标定：P1 δ=0 全落人工裁决，P2 禁止自动晋升"""

class SignificanceTester:
    def test(self, base: MetricSeries, cand: MetricSeries, alpha=0.05) -> Significance  # scipy.stats

class RegularizedSelector:       # FR9.3
    def decide(self, base, cand, ctx: DecisionCtx) -> Decision
        """主指标=加权综合分：S = Σ wᵢ·(Δmᵢ/σ_base)，max 方向为正、min 方向取负，
           σ_base 取基线噪声带标定的指标标准差（多目标默认合成规则；
           多目标冲突场景仍走 Q7 人工裁决，守卫分项不被总分补偿）。
           规则序（按序短路）：
           0. 候选批缺试率 > 30% → ADVISE_MANUAL（防环境故障误判为候选劣化，v1.5 增）
           1. safety.fatal 或 hack → REJECT
           2. 非补偿守卫：任一 guard 指标（missed_check/violations/high_risk_actions）恶化超噪声明 → REJECT
           3. 主指标 Δ ≤ δ（未标定 δ=0）→ REVERT(tie)
           4. 成本规则：Δ主指标增益 < cost_penalty*(Δtoken+Δcalls+Δtime 归一) → REJECT
           5. 显著性通过 → PROMOTE（P2 自动晋升前置条件=噪声带已标定，否则落 6）
           6. 其余 → ADVISE_MANUAL（附历史对比，Q7 人工裁决）"""
    # DecisionCtx = {base_stats, cand_stats: BatchStats, noise: NoiseBand | None,
    #   goals, guards, cost{Δtokens,Δcalls,Δwall_time}, hack_or_fatal: bool,
    #   recent_experiments: [...]}   # 选择器唯一输入（v1.5 增）

class ExplorationController:     # FR9.6（P2）
    def should_switch(self, recent_experiments) -> bool    # 连续 N 轮无收益
    def make_plan(self, lineage, attribution) -> ExplorationPlan
        # 定向选题 / 谱系重选父代（返回非基线 version 作为 iteration 起点）/ prompt 风格随机化标记
```

## 13. LLM 客户端与提示词库（llm 模块，主设计 3.12，v1.6 增）

```python
class LLMClient:                 # 统一入口（ADR-1/7/10 落地件）
    async def complete(self,
                       component: Literal["attribution","hypothesis","critic","judge","sft"],
                       prompt_version: str, vars: dict,
                       schema: dict | None = None) -> LLMResponse
    # 流程：PromptLibrary 渲染 → CassetteStore（命中→回放；未命中→provider 调用）
    #      → schema 提供时 JSON 校验 retry≤2 → 记账 llm_calls → 返回
    # 校验耗尽 → raise LLMOutputInvalid（调用方降级人工，ADR-1）

class PromptLibrary:             # 提示词模板（Git prompts/ 平台域，非 SUT 优化对象，FR5.7）
    def render(self, component: str, name_version: str, vars: dict) -> str      # {{var}} 渲染
    def list_versions(self, component: str) -> list[str]

class CassetteStore:             # 录制回放（ADR-7）
    # 键 = (component, prompt_version, sha256(渲染后输入))
    def get(self, key) -> LLMResponse | None    # replay 模式未命中直接 fail（测试策略）
    def put(self, key, response) -> None        # record 模式，资产 git 管理
```

---

> 修订记录：v2.6 — 第四轮审计文档-代码对齐（2026-11-09）：§9.6 `POST /batches/{id}:rerun` 补 rerun 批次语义——**复制原批次 purpose**，以 `params.rerun_of`（原批 id）与 `params.rerun_scope`（failed|all）标记（D-9，与实现一致；purpose 枚举无 rerun 值）。v2.5 — P0 多实例并行联动（设计 v2.13 / ADR-24）：labs.device_endpoints_json 语义改为适配器 (网络名,设备名)→(IP+端口) 映射（权威）+ env.ssh_access 基准校验；build_lab 多实例键 `<type>-<n>` P0 即用。v2.4 — 二轮交叉审查修复：DDL **optimization_profiles+iterations 前移至 batches 之前**（消除 batches.iteration_id/hypotheses.iteration_id/iterations.profile_id 前向外键——SQLite 宽容但 P1 PostgreSQL 建表即失败）；§9.12 budget_used `wall_h`→`wall_time_s`（与 Verdict.efficiency 命名统一）；§2.1 Verdict.efficiency 注明 estimated 透传。v2.3 — **平台 API 与 WEB 全量补齐（A–N）**：Router 17→**19 组**（新增 instances、audit-logs）；suts 增手动快照与**配置树文件 CRUD**（staging 编辑+commit 走 TargetRegistry 校验，P0 人工模式核心入口）；iterations 增 **:resume/DELETE（软删）/四个人工门端点**（confirm-direction/hypothesis/review/decision）与 budget_used；labs 增管理操作（建/销毁/重置/快照/恢复/只读 exec）；traces 增 :query/:stats（P1）；experiments 增 sft-report；dashboard 增 noise-bands；system 组扩展（settings 动态/静态管理与热更、DELETE :stop 熔断恢复、freeze 校验状态、logs 检索、health 组件级增强）；evalsets 增 site_overrides 与冻结版本列表；DDL hypotheses 增 iteration_id 列。v2.2 — 归因原文输入联动（设计 v2.10）：§12 AttributionAnalyzer.analyze 增 sut_config 参数（ConfigSnapshot，token 控制仅取相关文件原文）、AttributionReport.evidence 增 SUT 原文证据引用；HypothesisGenerator.stage2 输入"内容摘要"升级为"相关要素原文"。v2.1 — LLM 主判决层联动（设计 v2.9 / ADR-23）：§2.1 ExpectedAnswer.root_cause 契约改为 RootCauseExpectation（自然语言期望 + 可选 match_hints，语义判定归 LLMJudge P0）；checklist/risk_findings 标注语义命中判定。v2.0 — 删除故障编译层联动（设计 v2.8 / ADR-20 收尾）：DDL 删 fault_injections 表（22 张，其跟踪/清除职责由 ops.jsonl + restore_task 承担）；§4 SandboxService 删 inject_fault、Backend ABC 删 apply_fault（during_task P1 直接接受 CLI 动作再定契约）。v1.9 — 载体轨迹通道核实与分期联动（设计 v2.7 / ADR-22）：§8 增 wiretap 原文契约（per-run 文件、原帧永久保留、脱敏域界）；§10 适配器分期标注（P0=OpenCode 优先 / P2=ClaudeCode）。v1.8 — 自优化模式分期重构联动（设计 v2.6 / ADR-21）：DDL 增 iterations 表（mode/state/round 持久化）、batches 增 iteration_id 列（23 张表）；§9.12 iterations 改 **P0 交付** + mode 参数（p1/p2，人工模式不经端点）、§9.2 promote 扩展双批引用自动登记 experiments；§11 iterate_once 增 mode 参数。v1.7 — 评测准备模型重构联动（设计 v2.3 / ADR-20）：§2.1 EnvSpec 增 config_apis（外部配置 API 端点）、TaskRecord.prepare 改两种手段（exec_cli 多设备多命令 / call_api 组网名+场景名+devices）；§4 SandboxService prepare 重写 + 新增 restore_task（任务后恢复，按手段匹配 L1/L2）；§11 run_task 步骤扩至 9 步（备份在 prepare 内、恢复在判决后）。v1.6 — 七模块详细设计联动（设计 v2.2）：§5 增 TraceCollector 契约（serve_otel/tail_ndjson/ingest/finalize，EOF+宽限语义）；新增 §13 LLM 客户端与提示词库契约（LLMClient/PromptLibrary/CassetteStore）；§9 iterations 端点标注 P1 交付（P0 人工逐批驱动）。v1.5 — 子系统详细设计联动（设计 v2.1）：§9 重构为逐端点契约分表（9.0 统一约定+错误码表、9.1–9.17 端点契约、9.18 SSE/WS 事件信封与帧协议，ADR-18）；§12 增执行器协议（HypothesisSource/ReviewGate，ADR-16）、DecisionCtx、Selector 规则 0 与 P2 自动晋升前置条件、NoiseBandCalibrator 改显式标定批次语义（ADR-17）；DDL 增 noise_bands 表（22 张）、batches.purpose 增 noise_calibration。v1.4 — 二次交叉审查修复：OptimizationProfile 契约补全（全字段）并同步 DDL；StateAssert 补 expect_absent；DDL 增 checkpoints 表、batches 增 instrument/permission 列；eval_cases 更名 eval_tasks（case_key→task_id、yaml_path→source_ref）、runs.case_id→eval_task_id；§9 增 labs/iterations 路由及 pause/rerun/re-judge 端点（FR10.2/FR9.1）；§11 run_case→run_task（依赖改 TraceCollector、步骤重编号）；§12 TargetRegistry 冻结域注释修正、Selector 补加权综合分定义；§4 build_lab 后端键解析规则明确（与 env.yaml 枚举同名）。v1.2 — 评估集三件套重构（需求 v0.14）：§2.1 LabSpec/EvalCase 重构为 EnvSpec（env.yaml 契约，含 ssh_access 与站点覆盖规则）+ TaskRecord（tasks.jsonl 行契约，设备名引用）；§3 设备凭证来源改为 env.ssh_access 解析；§4 SandboxService 增 prepare；§11 run_case 流程改为"数据集级一次构建 + 任务级 restore/prepare"；labs 表注释同步；勘误：Hypothesis 字段名 case→task 对齐。v1.1 — §2 拆分为领域模型与元数据库 DDL。v1.0 — 自主设计文档 v1.5 拆分建档。
