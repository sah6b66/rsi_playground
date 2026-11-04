# 网络运维 Agent 的 RSI 演练场 配置设计文档

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DES-002 |
| 版本 | v1.4（P0 多实例并行联动） |
| 日期 | 2026-11-02 |
| 状态 | 待评审 |
| 上游文档 | 需求 RSI-PG-REQ-001 v0.25；设计 RSI-PG-DES-001 v2.12 |
| 关联文档 | 接口定义 RSI-PG-DES-003（docs/_rsi_playground_interfaces.md）；测试设计 RSI-PG-DES-004（docs/_rsi_playground_test.md） |

> 本文档承载**全部配置设计**（原设计文档第 5 章 5.1/5.2 内容）；主设计文档在需要的位置以"配置文档 §N"引用本文档。

---

## 1. 概述

**模块资源自管理约定（FR5.8，设计 ADR-12）**：不设全局节点注册表。各模块所需的资源（主机/SSH 接入/网段池/行为参数）在**自身配置段内自包含定义**——sandbox 在 `sandbox.backends.*` 定义每个仿真后端，carrier 在 `carrier.hosts.*` 定义 Docker 执行主机。同一物理主机被多个模块重复定义是**允许且有意的**（模块解耦优先）；跨模块的网段全局唯一性不靠运行时机制，由部署规划 + `deploy/check-subnets.ps1` 部署时校验保证（Q10）。

**配置段 ↔ 模块映射**（分组顺序即下文顺序）：

| 配置段 | 对应模块 | 设计文档章节 | 需求 |
|--------|----------|--------------|------|
| server / database / git / storage | 基础设施 | 2.2 / 2.3 | — |
| sandbox | rsi/sandbox | 3.4 | FR4、FR5.8 |
| carrier | rsi/carrier | 3.2 | FR1、FR5.8 |
| instrumentor | rsi/instrumentor | 3.3 | FR2.3、FR5.3 |
| tracing | rsi/tracing | 3.5 | FR2 |
| judge | rsi/judge | 3.6 | FR7 |
| optimizer | rsi/optimizer | 3.7 | FR6 |
| decision | rsi/decision | 3.9 | FR9 |
| orchestrator | rsi/orchestrator | 3.1 | FR5 |
| llm | rsi/llm | ADR-7/ADR-10 | FR5.7 |
| security | rsi/security | 3.11 | NFR8 |
| scheduler / seed | 调度与可复现 | ADR-8 / NFR2④ | — |

---

## 2. 平台配置（`settings.py`，环境变量/`settings.yaml` 双源）

> **动态/静态分区（v2.11）**：下述带 `§动态` 标注范围内的项可经 WEB/API（`PUT /system/settings/{path}`）热更（写回 settings.yaml + 内存刷新 + 审计，TU-034）；其余为静态项（路径/DB/git/网络类，改文件 + 重启生效，API 侧只读 409）。动态范围：`sandbox.build_retry/ready_timeout_s`、`carrier.task_timeout_s`、`judge.*`、`optimizer.*`、`decision.noise/alpha/no_gain_rounds_default`、`orchestrator.budget/checkpoint_interval_s`、`instrumentor.policy`、`tracing.redact/archive`、`llm.providers/defaults`、`security.jwt_expire_hours`。

```yaml
# ════════ ① 基础设施（全局） ════════
# 路径基准：相对路径基于 RSI_HOME（默认 ./var）；优先级：环境变量 > settings.yaml > 默认值
server:
  host: 0.0.0.0                 # str，API 监听地址（P0）
  port: 8000                    # int，Platform API 端口（I-9，P0）
database:
  url: sqlite:///${RSI_HOME}/platform.db   # str，P0 SQLite；P1 换 postgresql://（NFR4）
git:
  repo: {path: var/git/platform-repo.git, bare: true}   # 裸仓：承载 pre-receive 冻结强制（设计 6.4，P0）
  worktrees: var/git/wt         # GitStore 临时 worktree 目录（设计 3.8，P0）
  author: {name: rsi-platform, email: rsi-platform@local}   # 提交身份，联动审计（NFR1④，P0）
storage:
  traces_dir: var/traces        # 轨迹 NDJSON 根目录（设计 2.3，P0）
  artifacts_dir: var/artifacts  # 判决/报告/SUT 日志目录（设计 2.3，P0）
  labs_dir: var/labs            # lab 初始配置实例化产物目录（设计 2.3，P0）
  recordings_dir: var/recordings # LLM cassette 录制资产目录（设计 2.3，P0）
  stop_file: var/STOP           # 外部停机开关（FR5.5，P0；存在即熔断，5s 轮询）

logging:                        # 统一结构化日志（设计 5.2，NFR5）
  dir: var/logs                 # 日志根目录；rsi-YYYYMMDD.log 按天轮转
  level: info                   # 默认级别（debug|info|warning|error）
  retention_days: 30            # 轮转保留天数
  error_split: true             # rsi-error.log 单独实时落盘
                                # 开发态彩色格式：环境变量 RSI_LOG_FORMAT=console

# ════════ ② sandbox —— 仿真环境抽象层（rsi/sandbox，设计 3.4，FR4） ════════
sandbox:
  backends:                     # 后端资源自包含定义（FR5.8 模块自管；键=backend 类型名或
                                # 多实例键 <type>-<n>（**P0 即用**，ADR-24——每实例=独立 VM/服务器，
                                # 网络名字空间独立，按 (网络名,设备名)→(IP+端口) 映射寻址）
    ensp-pro-1:                 # ENSP Pro 实例 1（P0；Q1 PoC 决定编排深度）
      host: 192.168.x.x         # str，Windows 主机管理网地址（SSH 目标）
      ssh_port: 22              # int，默认 22
      ssh_user: administrator   # str；凭证亦可改经 CredentialStore 引用注入（FR1.5）
      agent_cmd: pg-enspctl     # str，host-agent CLI 命令（设计 3.4.3）
      templates_dir: deploy/ensp-templates   # str，预置拓扑模板目录（ADR-6）
      subnets: [10.101.0.0/16]  # list，本后端设备管理网段池（全网唯一，check-subnets 校验）
    pnetlab:                    # PNETLab 后端（P0；Q2）
      host: 192.168.x.y         # str，Linux 主机地址
      ssh_port: 22
      ssh_user: root
      api: optional             # EVE-NG 兼容 REST 可选增强通道（设计 3.4.4；缺省走宿主 SSH）
      subnets: [10.102.0.0/16]
  build_retry: 2                # int，环境构建失败重建上限（FR5.5，默认 2）
  ready_timeout_s: 600          # int，就绪探测超时秒（FR4.6，默认 600）

# ════════ ③ carrier —— SUT 承载适配层（rsi/carrier，设计 3.2，FR1） ════════
carrier:
  task_timeout_s: 1800          # int，单任务执行上限=30min（NFR4；超时→缺试计 0 分，FR7.4）
  adapters:                     # 载体基底镜像 = CarrierAdapter.build_image 的 FROM（FR1.1，非 SUT 镜像本身）
    claude_code: {image_base: rsi/claude-code-runtime:1.x}
    opencode:    {image_base: rsi/opencode-runtime:1.x}
  hosts:                        # Docker 执行主机池（FR5.8 模块自管，carrier/hosts.py 消费）
    main: {docker: local, subnets: [10.100.0.0/16]}
                                 # P0 默认：主节点本机（Docker Desktop / npipe）；
                                 # subnets=本主机 lab 网段池（全网唯一，check-subnets 校验，Q10）
    # remote-01: {docker: ssh://user@host, subnets: [10.103.0.0/24]}   # P1：SSH 隧道 DOCKER_HOST

# ════════ ④ instrumentor —— 插桩流水线（rsi/instrumentor，设计 3.3，I-8） ════════
instrumentor:
  policy:
    prefer: [hooks, mcp_wrap, kb_proxy, source]   # list，注入方式优先级（侵入度低→高，I-8 阶段2）
    source_injection: true      # bool，允许源码级注入（PG-TRACE 标记块）；false 则仅配置级（P1 起生效）
    smoke_verify: true          # bool，注入后冒烟验证（事件产出+行为不变），I-8 阶段4，不可关闭；
                                # 冒烟任务=冒烟集（FR3.7，P0）；评估集未含冒烟集时降级=数据集首个任务

# ════════ ⑤ tracing —— 轨迹子系统（rsi/tracing，设计 3.5，FR2） ════════
tracing:
  collector: {listen: 0.0.0.0, grpc: 4317, http: 4318}   # OTLP 接收（FR2.2；SUT 容器可达，FR1.6 白名单）
  redact: {enabled: true, rules_path: var/redact-rules.yaml}         # 脱敏管道（FR2.7，P1）
  archive: {codec: zstd, hot_days: 90, keep_promoted_forever: true}  # 归档老化（FR2.6/NFR7，仅归档不截断）

# ════════ ⑥ judge —— 评估裁判（rsi/judge，设计 3.6，FR7） ════════
judge:                          # 提示词/安全规则在 Git judge/ 冻结域（设计 2.2/6.4），此处仅运行参数
  det_timeout_s: 120            # int，确定性判定单命令超时（FR7.1）
  llm_retry: 2                  # int，裁判 LLM 失败重试；超限→run 判 indeterminate 进人工队列（设计 3.6）
  pairwise_enabled: true        # bool，pairwise 对比判决开关（P1，FR7.2）

# ════════ ⑦ optimizer —— 优化引擎（rsi/optimizer，设计 3.7，FR6） ════════
optimizer:
  critic_max_repair_rounds: 5  # int，Critic 拒绝后修复循环上限（FR6.10，默认 5）
  sft:
    no_gain_rounds: 5           # int，SFT 准入：同缺陷类 Agent 侧连续无收益轮数（FR6.9，Q8）
    min_samples: 50             # int，SFT 准入：失败轨迹样本量门槛（FR6.9，Q8）

# ════════ ⑧ decision —— 迭代决策（rsi/decision，设计 3.9，FR9） ════════
decision:
  noise: {k_repeats: 3, factor: 1.96}   # 噪声带：同任务重复 k 次标定 δ=σ×factor（FR9.2）
  alpha: 0.05                   # float，显著性检验水平（FR9.2）
  no_gain_rounds_default: 5     # int，收敛停机 N 全局默认（FR9.4；profile.stop.no_gain_rounds 可覆盖）

# ════════ ⑨ orchestrator —— 编排引擎（rsi/orchestrator，设计 3.1，FR5） ════════
orchestrator:
  max_parallel_batches: 2       # int，全局并行批次上限（v2.14 合并原 batch_concurrency/max_parallel_labs；
                                # 同 lab 内固定串行——共享设备状态；并行=跨批次，各自独立 lab；
                                # 受沙箱实例数约束；同 evalset 并行路由不同实例，ADR-24）
  checkpoint_interval_s: 5      # int，断点保存最小间隔秒（FR5.5）
  budget:
    default_iteration: {max_tokens: 50000000, max_wall_hours: 8, max_cost_usd: 200}   # 默认 scope 预算（FR5.6；profile.budget 可覆盖）
    global: {max_tokens: 100000000, max_cost_usd: 400}   # 全局预算（v2.14：跨批次聚合上限；
                                                          # 默认=单轮×max_parallel_batches；超限→全局 STOP 熔断）

# ════════ ⑩ llm —— 平台 LLM（rsi/llm，ADR-7/ADR-10） ════════
llm:                            # 仅平台侧四个 LLM 点（归因/假设/Critic/裁判）；SUT 的模型配置在
  providers:                    # /agent-config/model.json（FR1.8），二者勿混淆
    anthropic:     {api_key_env: ANTHROPIC_API_KEY}
    openai_compat: {api_key_env: OPENAI_COMPAT_API_KEY, base_url_env: OPENAI_COMPAT_BASE_URL}
  defaults:
    judge:       {model: "...", temperature: 0.0}   # 裁判温度锁定（ADR-10/NFR2）
    attribution: {model: "..."}
    hypothesis:  {model: "..."}
    critic:      {model: "..."}
  cassette_mode: replay         # replay|record|off，LLM 录制回放（ADR-7，测试用）
  max_concurrent_calls: 4       # int，LLM 调用全局并发信号量（v2.14：多批次并行防 rate limit；
                                # 优先级排队 judge>hypothesis>attribution>critic>sft）

# ════════ ⑪ security —— 认证与凭证（rsi/security，设计 3.11，NFR8） ════════
security:
  jwt_secret_env: RSI_JWT_SECRET   # JWT 签名密钥（仅环境变量，不入库不入仓）
  master_key_env: RSI_MASTER_KEY   # CredentialStore Fernet 主密钥（仅环境变量）
  jwt_expire_hours: 12             # int，token 有效期（设计 3.11）

# ════════ ⑫ 调度与可复现（ADR-8 / NFR2④） ════════
scheduler: {kind: asyncio, redis_url: redis://...}   # P0/P1=asyncio；P2 切 rq（ADR-8）
seed: {master: 20261102}         # int，全局主种子，三段派生（NFR2④）
```

---

## 3. 优化配置方案 Schema（OptimizationProfile，FR6.1，即 WEB 表单后端）

```yaml
name: bgp-accuracy-first          # str，优化配置方案名（WEB 表单对应，FR6.1）
evalsets:                         # 四分评测集绑定（FR3.7）——本轮迭代使用的三套集合 ID
                                  # （泛化集仅参考展示、不参与迭代与晋升，故不绑定）
  iterate: bgp-faults-iterate     # 迭代集：归因分析 + 候选预筛（不用于晋升判定）
  holdout:  bgp-faults-holdout    # 主评测集：晋升判定唯一依据（冻结，FR3.3）
  smoke:    core-smoke            # 冒烟集：候选活性检查（每域 3–5 条）
goals:                            # 优化目标与权重（FR6.1①；加权综合分定义见接口文档 §12 Selector）
  - {metric: root_cause_accuracy, direction: max, weight: 0.7}
  - {metric: avg_tokens, direction: min, weight: 0.3}     # 成本类目标（与 FR9.3 成本规则联动）
enabled_targets: [system_prompt, skills, knowledge, model_config]   # 本次允许优化项（白名单八项内勾选，FR6.1②；未勾选项执行层强制排除）
guards: {missed_check_worsen: reject, safety_any_worsen: reject, high_risk_worsen: reject}
                                 # 非补偿守卫开关（FR9.3；漏检/安全违规/高危动作任一分项恶化即一票否决）
stop:                             # 终止条件（FR9.4；覆盖 decision.no_gain_rounds_default）
  max_rounds: 20                  # int，单次运行最大迭代轮数
  no_gain_rounds: 5               # int，连续无显著收益 N 轮 → 收敛停机/切换探索模式（FR9.6）
budget: {max_tokens: 50000000, max_wall_hours: 8}   # 本方案预算（FR5.6；覆盖 orchestrator.budget.default_iteration）
llm_compare: {candidates: [claude-sonnet-4-6, glm-5]}   # switch_model 假设的候选模型池（FR6.3/FR1.8）
```

---

> 修订记录：v1.4 — P0 多实例并行联动（ADR-24）：backends 示例键改 ensp-pro-1（多实例键 `<type>-<n>` P0 即用，每实例独立 VM/网络名字空间）；batch_concurrency 默认 1→2（并行可配）+ 新增 max_parallel_labs（含同 lab 任务互斥注记——lab 内共享设备状态）。v1.3 — 二轮交叉审查修复：smoke_verify 冒烟任务来源对齐 FR3.7 P0（P0 即绑定冒烟集，未含时降级首个任务）。v1.2 — 二次交叉审查修复：后端配置键与 env.yaml backend 枚举同名（ensp_pro→ensp-pro）并注明多实例键规则；carrier.hosts.main 补 subnets 字段（FR5.8/Q10）；新增 logging 配置段与 labs_dir/recordings_dir（对齐设计 5.2/2.3）；guards 补 high_risk_worsen（对齐 FR9.3/Selector 规则 2）；smoke_verify 注明 P0 冒烟任务来源；goals 注明加权综合分定义位置。v1.0 — 自主设计文档 v1.5 拆分建档（原设计文档 5.1/5.2 内容整体迁入，内容未变）。
