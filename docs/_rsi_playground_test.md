# 网络运维 Agent 的 RSI 演练场 测试设计文档

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DES-004 |
| 版本 | v2.4（ADR-27 会话双模式联动） |
| 日期 | 2026-11-10 |
| 状态 | 待评审 |
| 上游文档 | 需求 RSI-PG-REQ-001 v0.26；设计 RSI-PG-DES-001 v2.13 |
| 关联文档 | 接口定义 RSI-PG-DES-003（被测接口签名/数据契约）；配置设计 RSI-PG-DES-002（测试相关配置：cassette_mode/seed） |

> 本文档承载**测试策略与用例设计**（原主设计文档第 7 章），含分层策略、测试基础设施（Fake 全家桶）、63 条用例清单（TU/IN/EE/SIM，映射需求验收）与 CI 流水线；主设计文档第 9 章追踪矩阵中的用例 ID 定义以本文档 §3 为准。

---

## 1. 分层与占比

| 层 | 范围 | 工具 | 占比 |
|----|------|------|------|
| 单元测试 | 状态机、判定 DSL、安全规则、Selector、噪声带、GitStore、脱敏、TargetRegistry、overlay 生成、seq 对齐 | pytest（无 Docker/无网络/无 LLM） | ~60% |
| 组件集成 | BatchRunner×FakeSandbox×FakeCarrier×InMemTrace 全链线；instrumentor 对 fixture SUT 目录；judge 对录制轨迹；FastAPI 路由（httpx AsyncClient） | pytest-asyncio | ~25% |
| E2E | docker-compose 起平台 + FakeSandbox + 真实示例 SUT（P0=OpenCode，含一个 echo MCP 工具；Claude Code P2）跑全链路；关键页面 playwright 冒烟 | compose + playwright | ~10% |
| 仿真回归（nightly/手动） | 真实 ENSP Pro / PNETLab 后端：模板构建→故障注入→就绪→复现性双跑 | pytest -m sim | ~5% |

**LLM 测试策略（设计 ADR-7）**：`llm/cassettes/` 录制（prompt_version + 输入 hash → 响应）；CI 全部 replay；提示词变更=新版本号=需重录，未录制的版本在 replay 模式直接 fail——保证提示词演进始终有回归资产。

---

## 2. 测试基础设施（tests/conftest.py）

```python
fake_sandbox       # FakeSandboxBackend：规则表驱动的虚拟设备（含 bad_config 反映）
fake_carrier       # cassette 回放任务结果 + 内存轨迹事件
inmem_trace        # TraceStore 内存实现
fake_gitstore      # 内存版 GitStore（语义等价，加速单测；谱系测试用真实临时仓库）
llm_cassette       # replay/record 双模式 fixture
fixture_sut_oc     # OpenCode 最小示例 SUT 配置树（1 skill + 1 MCP(echo) + 知识库 1 条）
                   # sut_demo_cc → P2（ADR-22：OpenCode P0 优先，Claude Code P2）
```

---

## 3. 关键测试用例清单（映射需求验收，指导用例编写）

| 用例 ID | 层 | 被测对象 | 步骤要点 / 预期 | 需求 |
|---------|----|----------|----------------|------|
| TU-001 | 单元 | BatchRunner 状态机 | 全部合法转换 + 非法转换拒绝（env_failed 重试≤2、timeout 不重试） | FR5.1/5.5 |
| TU-010 | 单元 | 判定 DSL（客观断言） | final_state 断言 contains/absent、critical_steps 证据命中计数、缺试计 0 分分母保留（语义判定见 TU-032/TU-026） | FR7.1/7.4 |
| TU-011 | 单元 | SafetyScanner | fatal 规则全表（reset saved-configuration、只读实例 system-view 等）逐条命中；正常查询零误报 | FR7.3 |
| TU-012 | 单元 | VerdictAggregator | fatal 一票否决、hack→0 分、path suspect+结论对→不通过 | FR7.4/7.5 |
| TU-013 | 单元 | TargetRegistry | 八个 target 路径域正反例；冻结域越界拒绝 | FR6.1/6.4 |
| TU-014 | 单元 | RegularizedSelector | 规则序 1–6 逐条短路验证（含噪声带内 tie、成本规则拒绝、守卫否决） | FR9.3 |
| TU-015 | 单元 | NoiseBandCalibrator | 合成方差数据 → δ 计算；显著性检验 alpha 生效 | FR9.2 |
| TU-016 | 单元 | GitStore | snapshot→commit→promote(tag fast-forward)→rollback→lineage 一致；verify_freeze 篡改检出 | FR8.1–8.3/7.6 |
| TU-017 | 单元 | OverlayBuilder | 配置级注入（mcp.json 包装/hooks）；源码标记块幂等注入/移除；原始目录零改动 | FR2.3/ADR-4 |
| TU-018 | 单元 | Redactor | 凭证指纹掩码；脱敏不改变其余内容完整性 | FR2.7 |
| TU-020 | 单元 | Critic 确定性层 | 越界/冻结域/未声明捆绑 拒绝路径 | FR6.10 |
| TU-021 | 单元 | 模块资源探测/check-subnets | 各模块资源就绪探测（后端 SSH 可达/主机 Docker 可达）；网段冲突校验脚本正反例 | FR5.8 |
| TU-022 | 单元 | 命名规范校验器 | 命名规范全部 ID/路径/tag 规则正反例；运行态 ID 时间戳段（UTC 14 位 YYYYMMDDHHmmss）与随机段格式校验；非法命名创建即拒 | 命名规范（主设计 2.5） |
| TU-023 | 单元 | PrepareAction 编译（ADR-20；v2.4：call_api 部分随 ADR-27 删除） | exec_cli 多设备多命令展开；设备名→ssh_access 端点解析；未知设备名拒绝 | FR3.1 |
| TU-030 | 单元 | 任务后恢复策略（ADR-20；v2.4：call_api 撤销路径删除，API 模式恢复由 TU-038/IN-023 覆盖） | exec_cli：prepare 前备份生成、判决后按厂商模板恢复（清空→重放备份）；L1 失败重试≤2→L2 环境级恢复切换 | FR4.4/FR5.2 |
| TU-024 | 单元 | CredentialStore | Fernet 加解密 round-trip；password_ref → ssh_access 解析；凭证注入时指纹登记（供 Redactor） | FR1.5 |
| TU-025 | 单元 | 站点覆盖校验 | env.\<site\>.yaml 仅允许覆盖 backend/ssh_access；越界字段（任务/期望答案）拒绝加载 | FR3.1/3.3 |
| TU-026 | 单元 | 判定（巡检/预检语义命中，cassette） | LLMJudge checklist/risk_findings 语义命中判定（漏检/误报计数）cassette 回放；JSON schema 校验与 retry 路径 | FR3.2/7.2（ADR-23） |
| TU-027 | 单元 | IterationEngine 状态机 | ATTRIBUTING→…→DECIDING 全部合法转换 + 非法转换拒绝（显式转换表，设计 3.1.2） | FR5 |
| TU-028 | 单元 | AntiHackDetector | critical_steps 证据命令匹配（偷步检出）；结论正确+路径 suspect → 不通过 | FR7.4 |
| TU-029 | 单元 | TaskResult 提取 | OpenCodeAdapter stdout 末块 FINAL_JSON 提取成功/失败路径；失败→"输出不可解析"失败模式（A 类样本；ClaudeCodeAdapter 提取 P2，ADR-22） | Q6 |
| TU-031 | 单元 | wiretap 解析器 | prompt-logger 帧映射（P0）：assembled(kind=context)→llm_request 全文（system/messages/tools）、http(kind=primary)→原始 body、协议差异（chat-completions `.messages[0].content` / responses `.instructions`）、kind 命名差异；stream-json 帧映射与 result 帧 usage、extended-thinking 过滤【P2 部分】 | FR2.2/附 C |
| TU-032 | 单元 | root_cause 语义判定（cassette） | LLMJudge 根因语义比对（ADR-23）：同义表述判对、无关/错误判错、证据不足→indeterminate；match_hints 仅用于报告高亮不影响判定；JSON schema 校验与 retry≤2→降级人工；**温度 0 稳定性：同输入 cassette 重判 N 次输出一致** | FR7.2 |
| TU-033 | 单元 | 归因原文对照（规则层） | FeatureExtractor 对照 SUT 原文：漏查步骤在提示词已写/未写 → 不同失败模式标签；幻觉命令知识库有/无 → 执行问题/知识缺失；B 类标注附原文证据引用（文件+位置）；token 控制：仅取 suspected_elements 相关文件 | FR6.2/6.9 |
| TU-034 | 单元 | settings 管理 | 动态项 PUT 热更生效（写回 settings.yaml+内存刷新+审计）；静态项 PUT→409 指引；GET 返回 source/mutable 标注 | FR10.1/v2.11 |
| TU-035 | 单元 | exec_readonly 白名单 | 前缀白名单内命令（display/show/ping 等）通过；白名单外（配置类/未知动词）拒绝；大小写与全路径变体 | FR7.1/设计 3.4.1 |
| TU-038 | 单元 | NetworkPrepClient（ADR-27，DD-04 §5.8） | prepare/restore/status 三方法请求组装与响应解析（mock httpx：端点路径/body 字段={params,session_id}/{env_id,params}/status→ready）；超时与非 2xx→EnvError（含响应体摘要）；status 探测语义——连接拒绝/超时/非 2xx→返回 False 不抛；base_url 空串→构造即 ConfigError | FR4.1/设计 3.13 |
| TU-039 | 单元 | API 评估集解析（ADR-27，DD-01 ApiEvalsetTask） | 评估集在线 API 响应 → ApiEvalsetTask 转换（tasks[] 逐行：network_env{name,params}/task{prompt}/expected{root_cause,final_state,critical_steps,checklist} 字段映射与缺字段校验拒绝）；与 CLI 类 TaskRecord 在 expected 判定维度同构 | FR3.1/设计 3.13 |
| IN-001 | 集成 | BatchRunner 全链线 | Fake 全家桶：3 任务批次 → 全部 done、Verdict/轨迹/checkpoint 落库 | FR5.1 |
| IN-002 | 集成 | 断点续跑 | 批次中断（第 2 个任务抛错）→ 重启恢复 → 仅补跑未完成任务 | FR5.5 |
| IN-003 | 集成 | 评测缓存 | 同 cache_key 二次执行 → status=cached 且不重跑；重判不重测 | FR9.1 |
| IN-004 | 集成 | 插桩流水线 | fixture SUT：扫描→计划→overlay→冒烟（事件产出+行为一致）→覆盖报告 | FR2.3/5.3 |
| IN-005 | 集成 | pg-mcp-proxy | 包装 echo MCP：tools/call 透传 + tool_call 事件记录 + 代理崩溃不影响原语义 | I-8 |
| IN-006 | 集成 | 优化迭代（cassette） | 归因（含 SUT 原文输入与 A/B 原文证据引用）→假设→Critic→apply→分级评估→PROMOTE 全线；拒绝分支→人工队列 | FR6.3–6.10 |
| IN-007 | 集成 | SFTAssessor 准入 | N 轮无收益模拟 → 报告生成；未达门槛 → None | FR6.9 |
| IN-008 | 集成 | API+RBAC | 各角色端点权限矩阵逐格验证；写操作进 audit_log | NFR8 |
| IN-009 | 集成 | SSE/WS | 状态变更推送到达；轨迹流尾部 N 条先发 | FR10.3 |
| IN-010 | 集成 | 资源选择 | 批次指定 sandbox.backends 键 → lab 落对应主机且 labs.host 正确；资源不可达→拒绝并报错 | FR5.8 |
| IN-011 | 集成 | 版本对比 | `/experiments/{id}/compare` 返回三段内容（diff/任务对照/trace-diff 引用）；TraceDiffer 对构造好的差异 run 对产出正确对齐 | FR10.8 |
| IN-012 | 集成 | 环境复用与任务后恢复 | 3 任务数据集：build_lab 仅调 1 次；任务 1 判决后设备状态被恢复（备份恢复/撤销）且任务 2 prepare 前环境干净；prepare/恢复失败→按手段恢复重试→仍失败计 0 分（env_failed）且任务 3 正常执行 | FR3.1/FR5.1 |
| IN-013 | 集成 | 预算与熔断 | BudgetController 超限 raise + halt 熔断；var/STOP 文件与 `POST /system:stop` 轮询即时生效 | FR5.5/5.6/NFR1⑤ |
| IN-014 | 集成 | wire 级采集完整性 | OpenCode wiretap：assembled→llm_request 含完整 system+messages+tools、http→原始 body（不截断）、per-run 文件归属正确；多源事件经 collector 统一 seq 单调（Claude Code 三通道断言 P2，附 C） | FR2.1/2.2/NFR2⑤ |
| IN-015 | 集成 | 资源回收 | lab 超时自动销毁、批次结束自动回收；labs.status=destroyed 落库 | FR4.7 |
| IN-016 | 集成 | 轨迹-版本反查 | run_id→runs→batches.config_version→Git 文件树全链反查；TraceStore.query/stats 过滤维度验证 | FR8.5/2.5 |
| IN-017 | 集成 | 评估集导入导出 | 三件套原格式导入→git 管理→导出 round-trip 内容一致 | FR3.5 |
| IN-018 | 集成 | LLMJudge 全链（cassette） | P0（ADR-23）：root_cause 语义/路径质量/checklist 命中/结论-过程一致性 → Verdict 聚合一票否决；pairwise 新旧轨迹对比报告（P1 部分） | FR7.2/7.5 |
| IN-019 | 集成 | 自优化模式（ADR-21） | P0 人工模式：人工两批（基线/候选）→ 人工 promote（双批引用+理由）→ experiments 自动登记证据链；P1 模式：iterations 全状态流转含 smoke 失败短路→REVERT 不进全量、人工门等待/通过；mode=p2 于 P2 开放前返回 409 | FR10.2/ADR-21 |
| IN-020 | 集成 | 人工门工作流端点（v2.11） | 四门推进全链：confirm-direction→hypothesis（写 hypotheses.iteration_id）→review→decision；非法状态（门序错乱）409；每门写 audit_log；:resume/DELETE（进行中 409 先 abort） | FR10.2 |
| IN-021 | 集成 | 运维观测端点集（v2.11） | instances 列表/清理；DELETE :stop 熔断恢复后批次可续；/system/logs 按 run_id 检索命中；/audit-logs 过滤；/system/freeze 与 health 组件级；SUT files CRUD（staging→commit 走 TargetRegistry 校验、越界 409）；sft-report/noise-bands/evalsets versions 返回 | FR10.1/10.9 |
| IN-022 | 集成 | 多实例并行（ADR-24） | 双批并行（不同沙箱实例）：lab/轨迹/判决互不干扰且各自正确；同 evalset 并行路由不同实例（(网络名,设备名)→(IP+端口) 映射唯一）；SshPool 会话按 (ip,port) 隔离（跨沙箱同名设备不串线）；同 lab 内任务串行（设备状态互斥）；SQLite WAL 并发写无 locked 错误；同一 SUT 二次迭代→409 互斥 | FR1.7/FR5.4/ADR-24 |
| IN-023 | 集成 | API 模式会话端到端（ADR-27） | mock 评估集在线 API + mock 网络准备 API + Fake carrier：run_batch(prep_mode="api") 全链——在线拉取评估集→NetworkPrepClient.prepare（env_id 返回+status 轮询 ready）→SUT 实例创建（**断言不注入 DEV_* 环境变量、不调 SandboxService.build_lab/prepare**）→执行→轨迹→判决→restore(env_id)→checkpoint/BatchStats 落库；prepare 失败重试≤2→env_failed | FR3.1/FR4.1/FR5.2/ADR-27 |
| EE-001 | E2E | P0 验收① | OpenCode 示例 SUT 按镜像规范构建、两配置版本切换产出可区分轨迹（Claude Code 载体 P2，ADR-22） | FR1.1/1.2/1.4 |
| EE-002 | E2E | P0 验收② | 10 任务数据集一键全链路（FakeSandbox）：构建→就绪→逐任务（恢复→prepare）→执行→轨迹→判决→归档 | FR5.1 |
| EE-003 | E2E | P0 验收③ | 同用例双跑（缓存旁路强制重跑）：**客观断言（终态/安全/critical_steps）判定一致；语义判决双跑一致率落入噪声带或 ≥ 阈值**（ADR-23 双口径） | NFR2 |
| EE-004 | E2E | P0 验收④ | 任意历史版本一键回滚并以该版本重跑 | FR8.3 |
| EE-005 | E2E | P0 验收⑤ | 切换两个 LLM 配置各跑一轮 → dashboard 可对比 | FR1.8/9.1 |
| EE-006 | E2E | P0 验收⑥ | 失败轨迹 A/B 标注 + SFT 评估报告模板生成 | FR6.9 |
| EE-007 | E2E | 前端冒烟 | 登录→批次详情：拓扑图/轨迹时间线/diff 三组件渲染 | FR10 |
| EE-008 | E2E | 轨迹完整性 | 超长设备命令输出（>1MB）全量入库可检索回放；逐事件 ts 存在且 run 内单调；日志按 run_id 可全链检索；SUT 容器 stdout/stderr 随 run 归档（sut-logs/<run_id>.log） | FR2.6/NFR2⑤/NFR5 |
| EE-009 | E2E | 网络隔离 | SUT 容器内访问平台 API/mgmt 端口应失败；两个 lab 网络互不可达 | FR1.6/FR4.8 |
| SIM-001 | 仿真 | ENSP Pro | 模板构建→SSH 初始配置→故障注入→就绪探测（Q1 PoC 脚本即本用例） | FR4/Q1 |
| SIM-002 | 仿真 | PNETLab | 同上 + 快照降级等价性（reset+重放 vs 快照恢复终态一致） | FR4.4/Q2 |
| SIM-003 | 仿真 | 复现性 | 真实后端同用例双跑：客观断言判定一致、语义判决一致率达标（ADR-23 双口径） | FR4 验收 |

---

## 4. CI 流水线

- PR 快速档：ruff + mypy + eslint + vitest + pytest(unit, ~2min)；
- 合并档：+ integration + web build（~8min）；
- nightly：EE 全量（compose）+ sim 标记用例（需仿真节点 self-hosted runner，缺省 skip 并标注）。

---

> 修订记录：v2.4 — **ADR-27 会话双模式联动（2026-11-10）**：新增 TU-038（NetworkPrepClient 三方法+超时/异常/status 探测语义）、TU-039（API 评估集在线响应→ApiEvalsetTask 转换）、IN-023（API 模式会话端到端：mock API→prepare→SUT→restore→判决，断言不注入 DEV_*/不走 SandboxService）；TU-023 中 call_api 组装部分随 config_apis 废除删除；用例总数 60→63。v2.3 — P0 多实例并行联动（ADR-24）：新增 IN-022（双批并行互不干扰/端点映射唯一/SshPool (ip,port) 隔离/同 lab 任务锁）；用例总数 59→60。v2.2 — 二轮交叉审查修复：EE-003/SIM-003 改写为 ADR-23 双口径（客观断言一致 + 语义判决一致率）；TU-032 增温度 0 稳定性断言；新增 TU-035（exec_readonly 前缀白名单）；用例总数 58→59。v2.1 — 平台 API 与 WEB 全量补齐联动（设计 v2.11/A–N）：新增 TU-034（settings 动态/静态管理与热更）、IN-020（人工门四端点工作流）、IN-021（运维观测端点集：instances/STOP 恢复/日志/审计/冻结/健康/文件 CRUD/报告/噪声带）；用例总数 55→58。v2.0 — 归因原文输入联动（设计 v2.10）：新增 TU-033（归因原文对照：提示词/知识库有无 → 不同失败模式、B 类原文证据引用、token 裁剪）；IN-006 增归因原文输入与证据断言；用例总数 54→55。v1.9 — LLM 主判决层联动（ADR-23）：TU-010 改写（客观断言：终态/证据命中/缺试，语义判定移出）；TU-026 改为 LLMJudge 巡检/预检语义命中（cassette）；新增 TU-032（root_cause 语义判定 cassette：同义判对/indeterminate/hints 非判据）；IN-018 提前 P0 并扩全链断言（pairwise 留 P1 部分）；用例总数 53→54。v1.8 — 删除故障编译层联动（设计 v2.8）：TU-019（FaultCompiler）删除；分层表"FaultSpec→CLI 编译"移除；用例总数 54→53。v1.7 — 载体轨迹通道核实与分期联动（ADR-22）：新增 TU-031（wiretap 解析器：prompt-logger 帧映射 P0 / stream-json 帧 P2 部分）；EE-001/TU-029/IN-014 改为 OpenCode 为主断言对象（Claude Code 部分标 P2）；fixture 说明改 sut_demo_oc（P0）/sut_demo_cc（P2）；用例总数 53→54。v1.6 — 自优化模式分期重构联动（ADR-21）：新增 IN-019（P0 人工模式向导链路 + P1 模式状态流转与短路 + mode=p2 409）；TU-027（IterationEngine 状态机）与 IN-006（优化迭代链路）随设计提前为 P0 可执行；用例总数 52→53。v1.5 — 命名规范联动（设计 v2.4）：TU-022 增运行态 ID 时间戳段（UTC 14 位）与随机段格式校验。v1.4 — 评测准备模型重构联动（ADR-20）：TU-023 改写为两种手段编译（exec_cli 多设备多命令 / call_api 请求组装）；新增 TU-030（任务后恢复策略：备份恢复模板、API 撤销、L1/L2 切换）；IN-012 改为任务后恢复断言（判决后恢复、prepare 前环境干净）；TU-019 描述随故障库定位调整（编译为 exec_cli）；用例总数 51→52（TU 22）。v1.3 — 二次交叉审查修复：新增 13 条用例——TU-024（CredentialStore）/TU-025（站点覆盖校验）/TU-026（巡检·预检判定）/TU-027（IterationEngine 状态机）/TU-028（反偷步检测）/TU-029（FINAL_JSON 提取）、IN-013（预算熔断与 STOP）/IN-014（wire 级完整性）/IN-015（资源回收）/IN-016（轨迹-版本反查）/IN-017（导入导出）/IN-018（LLMJudge）/EE-009（网络隔离）；EE-008 补 SUT 日志归档断言；用例总数勘误 33→51（TU 21 + IN 18 + EE 9 + SIM 3）；IN-001/002 术语"用例"→"任务"。v1.1 — 评估集三件套重构配套：新增 TU-023（prepare 编译）与 IN-012（环境一次构建 + 任务间恢复）；TU-019 描述同步。v1.0 — 自主设计文档 v1.7 拆分建档（原第 7 章整体迁入，内容未变；TU-022 需求列引用同步为主设计 2.5 命名规范）。
