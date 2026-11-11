# 网络运维 Agent 的 RSI 演练场 配置设计文档

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DES-002 |
| 版本 | v2.0（配置三层模型：用户/内部/部署分离，C1-C2 落地） |
| 日期 | 2026-11-05 |
| 状态 | 待评审 |
| 上游文档 | 需求 RSI-PG-REQ-001 v0.25；设计 RSI-PG-DES-001 v2.12 |
| 关联文档 | 接口定义 RSI-PG-DES-003（docs/_rsi_playground_interfaces.md）；测试设计 RSI-PG-DES-004（docs/_rsi_playground_test.md） |

> 本文档承载**全部配置设计**（原设计文档第 5 章 5.1/5.2 内容）；主设计文档在需要的位置以"配置文档 §N"引用本文档。

---

## 1. 概述

**模块资源自管理约定（FR5.8，设计 ADR-12）**：不设全局节点注册表。各模块所需的资源（平台 URL/后端参数/设备网段池/行为参数——ADR-25）在**自身配置段内自包含定义**——sandbox 在 `sandbox.backends.*` 定义每个仿真后端，carrier 在 `carrier.hosts.*` 定义 Docker 执行主机。同一物理主机被多个模块重复定义是**允许且有意的**（模块解耦优先）；跨模块的网段全局唯一性不靠运行时机制，由部署规划 + `deploy/check-subnets.ps1` 部署时校验保证（Q10）。

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

## 2. 平台配置（三层模型 v2.0）

### 2.0 配置分层

| 层 | 载体 | 内容 | 修改方式 |
|----|------|------|---------|
| 部署环境变量 | 进程环境 | RSI_JWT_SECRET、RSI_MASTER_KEY（安全密钥）、RSI_HOME（数据根目录） | 部署时设置 |
| 用户配置 settings.yaml | 文件 + WEB 参数页 | ~45 项系统级与对象缺省参数 | WEB 在线编辑（热更或待重启）或直接改文件 |
| 内部配置 settings-internal.yaml | 文件（不进 WEB） | ~15 项开发运维项 | 改文件 + 重启 |

优先级：环境变量（前缀 RSI_，如 RSI_SERVER__PORT）> settings.yaml > 代码默认值。示例文件：`examples/settings.yaml.example` 与 `examples/settings-internal.yaml.example`（复制到 RSI_HOME 目录使用）。

### 2.1 用户配置（settings.yaml）

分组与 `settings.yaml.example` 一致；每键均为用户级可编辑（GET/PUT /system/settings）。

**服务**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| server.host | str | 0.0.0.0 | API 监听地址；127.0.0.1=仅本机 |
| server.port | int | 8000 | API 端口号（1-65535） |

**数据库**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| database.url | str | sqlite:///<RSI_HOME>/platform.db | P0 用 SQLite；P1 升 PostgreSQL 时改 postgresql:// |

**模型服务（LLM）**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| llm.defaults.judge.model | str | "" | 裁判模型名称；留空用回放/规则兜底（热更） |
| llm.defaults.judge.temperature | float | 0.0 | 裁判采样温度；0 = 尽量确定性 |
| llm.cassette_mode | str | replay | replay 回放录像 / record 录制 / off 真实调用（ADR-7） |
| llm.max_concurrent_calls | int | 4 | 模型并发调用上限（多批次并行防 rate limit） |

> 注记：`llm.providers.*`（api_key/base_url）不在参数页 dump——经专用 LLM 配置接口（`GET /system/settings/llm` 半脱敏 + admin `PUT`）或直接改 settings.yaml 管理（内网可信环境，明文直配）。

**裁判**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| judge.det_timeout_s | int | 120 | 确定性（规则型）评审单次执行超时（秒，FR7.1） |
| judge.llm_retry | int | 2 | 模型评审失败重试次数；超限→indeterminate 进人工队列 |
| judge.pairwise_enabled | bool | true | 同轮多方案时启用两两对比评审（FR7.2） |

**优化与决策**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| optimizer.critic_max_repair_rounds | int | 5 | 自动评审发现问题后的最大修复轮数（FR6.10） |
| optimizer.sft.no_gain_rounds | int | 5 | 连续无提升多少轮后允许整理 SFT 训练数据（FR6.9） |
| optimizer.sft.min_samples | int | 50 | 整理训练数据所需最低有效样本数（FR6.9） |
| decision.alpha | float | 0.05 | 统计检验显著性水平 α；越小越保守（FR9.2） |
| decision.no_gain_rounds_default | int | 5 | 连续无提升多少轮后触发收敛判定（FR9.4；profile 可覆盖） |
| decision.noise.k_repeats | int | 3 | 标定基线噪声的重复测量次数（FR9.2） |
| decision.noise.factor | float | 1.96 | 95% 置信对应的 σ 倍数（FR9.2） |

**编排**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| orchestrator.max_parallel_batches | int | 2 | 同时运行的评测批次数上限（同 lab 内固定串行，ADR-24） |
| orchestrator.checkpoint_interval_s | int | 5 | 断点续跑检查点存档间隔（秒，FR5.5） |
| orchestrator.health_monitor_interval_s | int | 30 | 运行环境健康巡检周期（秒） |
| orchestrator.budget.iteration_default.max_tokens | int | 50000000 | 每 RSI 会话缺省 Token 上限（FR5.6） |
| orchestrator.budget.iteration_default.max_wall_hours | int | 8 | 每会话墙上时长上限（小时） |
| orchestrator.budget.iteration_default.max_cost_usd | int | 200 | 每会话费用上限（美元） |
| orchestrator.budget.global.max_tokens | int | 100000000 | 全平台累计 Token 上限；超限→全局 STOP 熔断 |
| orchestrator.budget.global.max_wall_hours | int | 0 | 全平台累计时长上限；0 = 不限制 |
| orchestrator.budget.global.max_cost_usd | int | 400 | 全平台累计费用上限（美元） |

**载体缺省**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| carrier.task_timeout_s | int | 1800 | Agent 单任务最长执行时间（秒）；可热更（超时→缺试计 0 分，FR7.4） |
| carrier.max_instances_per_host | int | 10 | 未单独配置主机的 SUT 实例数上限；可热更（host 级可覆盖） |

**网络仿真缺省**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| sandbox.build_retry | int | 2 | 实验环境构建失败重试次数（FR5.5） |
| sandbox.ready_timeout_s | int | 600 | 等待仿真环境就绪超时（秒，FR4.6） |

**轨迹**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| tracing.redact.enabled | bool | true | 入库前按规则抹除密钥等敏感内容（FR2.7） |
| tracing.archive.hot_days | int | 90 | 热数据保留天数；超期转入冷归档（FR2.6） |

**日志**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| logging.level | str | info | debug / info / warning / error；可热更 |
| logging.retention_days | int | 30 | 超期日志自动清理 |
| logging.error_split | bool | true | 错误级别日志单独落盘便于排查 |

**可复现**

| 键 | 类型 | 缺省 | 说明 |
|----|------|------|------|
| seed.master | int | 20261102 | 随机种子：相同种子+相同输入 → 相同采样与裁决顺序（NFR2④） |

### 2.2 对象页管理（不在参数页呈现）

- `sandbox.backends.*` → 网络仿真平台页（每实例 kind/url/verify_tls/api/subnets，ADR-24/25；独立 GET/PUT 端点，改后 restart_required）
- `carrier.hosts.*` → 运行环境页（docker/subnets/max_instances 多主机池，v2.18 R1-R6；独立 GET/PUT 端点，改后 restart_required）

### 2.3 内部配置（settings-internal.yaml.example）

内部项只认 settings-internal.yaml（写入用户 settings.yaml 的同名键被忽略），不呈现于 WEB，修改后需重启。

**插桩策略**

| 键 | 缺省 | 说明 |
|----|------|------|
| instrumentor.policy.prefer | [hooks, mcp_wrap, kb_proxy, source_marker] | 注入方式优先级（侵入度低→高，I-8） |
| instrumentor.policy.source_injection | true | 允许源码级注入（PG-TRACE 标记块） |
| instrumentor.policy.smoke_verify | true | 注入后冒烟验证（事件产出+行为不变，不可关闭） |

**轨迹采集器与归档**

| 键 | 缺省 | 说明 |
|----|------|------|
| tracing.collector.listen / grpc / http | 0.0.0.0 / 4317 / 4318 | OTLP 接收（FR2.2；SUT 容器可达） |
| tracing.redact.rules_path | redact-rules.yaml | 脱敏规则文件路径 |
| tracing.archive.codec | zstd | 冷归档压缩编码 |
| tracing.archive.keep_promoted_forever | true | 已晋升轨迹永久保留 |

**调度器**

| 键 | 缺省 | 说明 |
|----|------|------|
| scheduler.kind | asyncio | P0/P1=asyncio；P2 可切 rq（ADR-8） |
| scheduler.redis_url | null | rq 调度时使用 |

**Git**

| 键 | 缺省 | 说明 |
|----|------|------|
| git.repo.bare | true | 裸仓形态（pre-receive 冻结强制，设计 6.4；仓库路径从 RSI_HOME 派生） |
| git.author.name / email | rsi-platform / rsi-platform@local | 提交身份，联动审计（NFR1④） |

**载体基座**

| 键 | 缺省 | 说明 |
|----|------|------|
| carrier.adapters.claude_code.image_base | rsi/claude-code-runtime:1.x | Claude Code 载体基底镜像（FR1.1） |
| carrier.adapters.opencode.image_base | rsi/opencode-runtime:1.x | OpenCode 载体基底镜像（FR1.1） |

### 2.4 派生路径

RSI_HOME（环境变量，默认 ./var）下自动创建：`git/`（裸仓+worktrees）、`storage/{traces,artifacts,labs,recordings}/`、`logs/`、`STOP` 文件（外部停机开关，FR5.5）。存储路径不再是用户配置键。

### 2.5 热更与重启

动态键 4 个即时生效：`carrier.task_timeout_s`、`carrier.max_instances_per_host`、`logging.level`、`llm.defaults.judge.model`。其余用户键保存后标"待重启"（pending_restart）→ WEB「重启平台」按钮统一生效（POST /system:restart，进程管理器重新拉起）。

### 2.6 兼容

旧 yaml 的 `storage.*`、`git.repo.path`、`git.worktrees`、`security.*`、`env` 名等派生/内部/废弃键被静默忽略；旧 `orchestrator.budget.default_iteration` → `iteration_default`；`global`（代码字段 `global_`，yaml 键 `global`）。
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

> 修订记录：v2.0 — **配置体系重构三层模型（ADR-26，C1-C2 落地，2026-11-05）**：§2 全量重写——部署环境变量（RSI_JWT_SECRET/RSI_MASTER_KEY/RSI_HOME）/用户 settings.yaml（参数页全键可编辑）/内部 settings-internal.yaml（~15 项，不进 WEB）三层分离；安全密钥仅环境变量；LLM api_key/base_url 内网明文直配（yaml）+ 专用半脱敏 LLM 配置接口；存储路径改由 RSI_HOME 派生（git/、storage/{traces,artifacts,labs,recordings}/、logs/、STOP）；对象注册（sandbox.backends.*/carrier.hosts.*）归对象页；动态键收缩为 4 个，其余标 pending_restart 经 POST /system:restart 统一生效；旧派生/内部键静默忽略，budget.default_iteration→iteration_default。v1.6 — **沙箱连通性模型重构（ADR-25 预建模式，2026-11-09）**：sandbox.backends.* 键形态重写——删 host/ssh_port/ssh_user/agent_cmd/templates_dir，新 kind（ensp-pro|pnetlab|fake）/url（平台 Web 入口，URL 探活）/verify_tls（自签默认关）/api（P1/S11 预留）/subnets（保留）；新增 fake 默认后端；增注记「平台不可 SSH；设备 SSH 凭证在 env.yaml ssh_access 段定义（同 IP 不同端口复用，设备 IP 可与平台 IP 不同）」。v1.5 — v2.18 远端 Docker 主机支持（R1-R6）：carrier.hosts 多主机完整示例（本地 main + 远端 gpu-01/gpu-02 SSH 隧道）；新增 max_instances_per_host（全局默认 + host 级覆盖）；InstanceScheduler least-loaded 选主机。v1.4 — P0 多实例并行联动（ADR-24）：backends 示例键改 ensp-pro-1（多实例键 `<type>-<n>` P0 即用，每实例独立 VM/网络名字空间）；batch_concurrency 默认 1→2（并行可配）+ 新增 max_parallel_labs（含同 lab 任务互斥注记——lab 内共享设备状态）。v1.3 — 二轮交叉审查修复：smoke_verify 冒烟任务来源对齐 FR3.7 P0（P0 即绑定冒烟集，未含时降级首个任务）。v1.2 — 二次交叉审查修复：后端配置键与 env.yaml backend 枚举同名（ensp_pro→ensp-pro）并注明多实例键规则；carrier.hosts.main 补 subnets 字段（FR5.8/Q10）；新增 logging 配置段与 labs_dir/recordings_dir（对齐设计 5.2/2.3）；guards 补 high_risk_worsen（对齐 FR9.3/Selector 规则 2）；smoke_verify 注明 P0 冒烟任务来源；goals 注明加权综合分定义位置。v1.0 — 自主设计文档 v1.5 拆分建档（原设计文档 5.1/5.2 内容整体迁入，内容未变）。
