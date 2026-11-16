# P1 阶段实现设计（L1+ 优化增强期）

- 日期：2026-11-16
- 状态：已批准（brainstorming 三轮分节确认）
- 上游权威：需求 v0.28 §8 P1 范围 / 设计 v2.20 / 接口 v2.4 / 配置 v1.6 / 测试 v2.2 / DD-00..14
- 基线：main `47f90e0`，1453 passed + 12 skipped

## 1. 背景与目标

P0（基础设施期，ADR-21）已交付全链路 + 半自动优化闭环，S11 真机验证于 2026-11-16 收官。P1 目标（需求 §8）：**LLM 增强归因与起草、统计置信、裁判升级**，并补齐轨迹域/执行域扩展能力。

P1 验收四条：

1. LLM 归因精分类与假设起草上线（提示词 cassette 回归），归因与假设建议质量可对比 P0 规则层基线度量；
2. 噪声带标定与显著性检验上线，Selector 决策建议附统计置信信息；
3. pairwise 新旧对比上线，至少拦截一类刷分案例（演示）；
4. 效果看板呈现成功率/步数/token 的版本间对比曲线。

其中第 2 条已随 P0 审计修复落地（`decision/significance.py`、`decision/selector.py:163-217`、`dashboard.py:304` 标定落库），本轮仅需验收确认与看板呈现补齐。

## 2. 关键决策记录（用户拍板，2026-11-16）

| # | 决策 | 内容 |
|---|------|------|
| D1 | 范围取向 | **全量一轮做完**：P1 全部 11 个特性域 + P0 遗留，不分期后置 |
| D2 | P0 遗留纳入 | store 落库、FK 完整性、doctor 措辞对齐；**LLM 健壮化经 DD-02 冲突确认后纳入**（DD-02 §5.1 明文退避重试 + backup_provider，不纳入即形成文档-实现偏差） |
| D3 | 验收形态 | **离线 + 真机验收**：全部实现离线全绿后，E:\rsi_test 真机跑一轮 P1 验收 |
| D4 | 实现路径 | **方案 A：打底先行 + 主线递进**——B1 打底 → B2 LLM 主线 → B3 轨迹域 → B4 WEB 呈现 → B5 执行域 → B6 真机验收 |

不列入（登记在案）：孤儿 judgment/僵尸批清理（部署数据非代码）、platform/examples/ 遗留片段（部署权威=根目录 examples/，另行清理）。

## 3. 现状基线（2026-11-16 探索核实）

| 特性 | 状态 | 证据 |
|------|------|------|
| LLM provider 真实接入 | ✅ | `llm/providers.py` Anthropic 兼容 handler；`bootstrap.py:240-246` 配 key+cassette off 即启用（S11 即此形态跑通 glm-5.1 裁判） |
| LLM 退避重试/备胎/真实 usage | ❌ | 单次 `httpx.post` 无重试；token 按 len//4 估算落 llm_calls |
| FR6.2/6.3 归因/起草 | 🟡 | 模板与 IterationEngine 接线在（iteration.py:651/:686），归因仍是规则层 PreClusterer（optimizer/attribution.py） |
| FR7.2 pairwise | ❌ | judge/ 零命中；DD-08:66 接口已定义 |
| FR9.2 显著性/噪声带 | ✅ | significance.py + Selector 集成 + 标定落库 |
| FR5.5 断点续跑 / FR5.6 预算熔断 | ✅ | checkpoint resume + BudgetController |
| FR10.4 看板 | 🟡 | metrics/model-compare 已真实聚合；defect-dist 仅读注入数据（dashboard.py:162-170） |
| FR10.6/10.8 轨迹查看/版本对比 | 🟡 | 单 run 时间线+跨轮双栏有；两版本快照级 diff 无；/traces/diff 骨架在（traces.py:92） |
| FR2.5/2.6/2.7 检索/存储/脱敏 | ❌ | 全库零命中；trace_files 表预留（db/tables.py:309） |
| FR1.7 实例池 | 🟡 | SSH 会话池有（sshpool.py:153-178），实例级池化无 |
| FR4.5 during_task | ❌ | 无 /labs/{lab_id}/actions（契约 P1 定义） |
| FR5.3 源码级插桩 | 🟡 | 仅固定路径扫描（instrumentor/scanner.py） |
| FR3.4 泛化集 | ❌ | 仅 purpose 枚举（db/tables.py:163），无消费链路 |
| W11 checklist 聚合 | ✅ | judge/aggregate.py:81-94 已按 expected 形态分派 |
| W12 traces 查询 | ✅（回退式） | 空时回退读 collector（runs.py:108）；非真正回灌 |
| suts/evalsets store | 🟡 | 仍内存态，重启清空需重跑 provision.py |
| runs/batches FK | ❌ | batch.py:1180-1181 evalset_id/sut_id 空串占位 |

## 4. 批次设计

### B1 打底批（P0 遗留四项；2026-11-16 计划期现状核实后收窄修订）

**1. store 持久化补齐**（重启不丢，退役 provision.py 依赖）
- 现状修正：F3 批（1a23aeb）已落对象文件库——suts/evalsets/iterations/profiles/hypotheses 经 persist_record 落 `<RSI_HOME>/storage/object-store/<collection>/<id>.json`，bootstrap.py:586-589 启动水合（同 id 不覆盖内存）；创建/导入/克隆/删除均已持久化，E:\rsi_test 部署侧已有数据，**无需迁移脚本**
- 剩余缺口：update_evalset（evalsets.py:254-272，es.update+version+=1 后无 persist）、`:freeze`、`:assign` 改记录不落盘——补 persist_record 调用
- DB 行同步：persist_record/delete_record 扩展对 suts/evalsets 同步 upsert/remove DB 行（表维持"存在性证明"语义，payload 不入 DB——内存 store 水合后即含 payload，resolver 现行读法不变）；跨表 join（B4 前置）由此完整

**2. FK 完整性**（根因已定位）
- 根因：引擎 VERIFYING 的 db_meta（iteration.py:815）只带 iteration_id/sut_id 缺 evalset_id → _db_insert_batch（batch.py:1181）空串兜底；API 路径（batches.py:94-102 store 校验）已带真实 ID 无需改
- 修复：iterate_once/_iterate_body 增 evalset_id 参数线程化至 db_meta（baseline/candidate 共用构造点）；_db_insert_batch 空串时 warning 留痕；外部驱动（session.py）传参更新随 B6 验收轮
- runs 落库 lab_id/eval_task_id 真实值核验补齐

**3. doctor 契约文档化**（措辞对齐方向翻转）
- 现状修正：DD-13 无 /system/doctor 契约段（grep 零命中）——契约只存在于 doctor.py 实现
- 文档先行：DD-13 补 doctor 响应契约段（CheckResult/DoctorReport 归纳）；实现侧对照上游五文档 doctor 相关表述（需求 FR10.9②"组件级健康检查——DB/Git 仓库/trace-collector/各仿真后端/冻结校验"）微调字段名/措辞

**4. LLM 健壮化**（DD-02 §5.1 明文）
- 退避重试：provider 调用包 retry 循环 `sleep(min(2**attempt, 30) + uniform(0, 1))`；429/5xx/网络错误触发；次数可配 `llm.retry_max`
- backup_provider：`llm.providers` 支持主备，主连败切换（DD-02:44,105-107）
- 真实 usage 回填：handler 返回携带 provider usage → LLMResponse.usage → llm_calls 表记真实 token（替换 len//4 估算）

### B2 LLM 主线（FR6.2/6.3/6.9 + FR7.2）

**1. FR6.2 LLM 精分类归因**
- 规则层 PreClusterer 产簇后追加 LLM 精分类（attribution 模板已就位）
- 输入：簇轨迹摘要 + SUT 配置版本原文（FR6.2 v0.23 归因输入契约）+ opt_history（排除已试无效方向）
- 输出：失败模式细类 + A/B 缺陷类别 + 原文证据引用 + Top-N 优化方向
- 降级：LLM 失败/未配 key 回退规则层结果，`attribution_source ∈ {rules, llm}` 落库可审计
- 接线：IterationEngine attributing 阶段（iteration.py:651 现有调用点扩展）

**2. FR6.3 LLM 假设起草**
- 人工门①确认方向后提供 LLM 起草（hypothesis 模板）：按 FR6.3 schema 输出结构化假设草稿（含 predicted_affected_tasks 预测清单）**预填人工门表单**，人工可改后提交
- 半自动语义：人工仍在环，不跳门；LLM 起草是可选增强按钮而非自动提交
- 决策阶段回算预测命中率已实现（iteration.py:1058 `_calc_hit_rate`，DD-12 §5.5），本轮无需补

**3. FR6.9 归因驱动半自动分类**
- A/B 分类从纯人工改为 LLM 建议 + 人工确认
- SFT/DPO 评估报告的缺陷分布/证据引用段由归因数据自动填充（模板保留人工复核）

**4. FR7.2 pairwise 对比判决**
- 按 DD-08:66 `pairwise(baseline_run, candidate_run) -> PairwiseReport`
- 同任务两 run 轨迹摘要并排 prompt（新增 judge/pairwise 模板），输出 winner + 维度理由 + 刷分嫌疑标记（路径异常/证据不足/绕过验证步骤的答对模式）
- 定位：对比增强，不改变 ADR-23 单 run 主判决；消费方 = Selector 建议 + 版本对比页（B4）
- 验收演示：构造一类刷分案例被拦截

**cassette 回归**：attribution/hypothesis/pairwise 三组件 record/replay 测试随实现落地（验收标准明文）；改模板后按既有规程重跑 freeze_check。

### B3 轨迹域（FR2.5/2.6/2.7 + FR10.9⑥）

**1. FR2.5 / FR10.9⑥ 跨 run 检索与统计**
- TraceStore 扩展 query：按任务/失败类型/版本/时间过滤——trace_events_index 与 runs join 聚合；现有 `POST /traces:query`（traces.py:127）扩展过滤维度
- `cluster_failures`（DD-07:67）：按失败特征规则聚类，供 FR6.2 消费；新增 `POST /traces:cluster`（接口文档 §2 已预留）
- W12 回退式读取保留为兜底，DB 索引成为权威查询路径

**2. FR2.7 脱敏管道**
- `Redactor`（DD-07:74）按 SecretFingerprint 掩码：env.yaml ssh_access 凭证 + settings 凭证字典 + 通用正则（密码/API key 模式）
- 管道位置（DD-07:53）：摄取后、seq 分配前脱敏；events.ndjson/索引写脱敏后内容；**wiretap 原文不动**（审计原文）
- 开关可配 `traces.redact.enabled`（默认开），关闭时审计日志留痕

**3. FR2.6 存储管理**
- zstd 归档：finalize 超过 `traces.hot_days`（可配）的 run 目录压缩（DD-07 注记④），在线数据不动
- 老化：归档超 `traces.cold_days` 可清理，默认只归档不删；doctor/system 报告存储量

### B4 WEB 呈现（FR10.4/10.6/10.8）

**1. FR10.4 看板补齐**
- defect-dist 改从 runs/judgments 真实聚合（现仅读注入数据）
- 新增版本间对比曲线（验收明文）：metrics 按 config_version 分组——成功率/步数/token 三指标版本对比图（依赖 B1-FK）
- 噪声带标定状态（σ/δ/有效期）看板展示

**2. FR10.6 轨迹查看器增强**
- 现有 TraceTab（单 run 时间线 + 跨轮双栏）增强：LLM 请求/响应全文展开、工具调用入出参折叠展开（消费 B3 检索产物，脱敏后内容）

**3. FR10.8 版本对比分析（新视图）**
- 后端 `TraceDiffer`（DD-07:69）：两版本同任务 run 对的事件级对齐（LLM 请求/工具序列 diff）；`GET /traces/diff`（traces.py:92）扩展两版本模式 + 版本对比汇总端点
- 前端三栏：① 配置文件级 diff（复用版本管理 git diff）② 轨迹并排差异 ③ 任务级对照表（改善/恶化/持平标注、指标 delta、判决详情跳转）
- 统一命名规范互查（设计 2.5）

### B5 执行域（FR4.5/FR1.7/FR5.3/FR3.4）

**1. FR4.5 during_task 动态执行**（契约本轮定义）
- `POST /labs/{lab_id}/actions {actions[], run_id?}`：任务执行期间经 SandboxService 下发 exec_cli 动作（动态扰动/追加指令，评估 agent 鲁棒性）
- 安全：复用 F13 管理口保护（GE1/0/10 视图 fatal）+ read-only 冻结域校验；操作写 ops.jsonl 进恢复链；审计留痕
- 契约先落接口文档 §9 再实现

**2. FR1.7 实例池复用**
- HostService/carrier 增实例池：run 结束实例保留（空闲 TTL 可配），下个 run 命中同 (sut_id, config_version) 复用，失配销毁重建
- 池内实例打标签与 F12 孤儿清扫区分（不误杀）；池大小/超时 settings 可配

**3. FR5.3 源码级插桩**（DD-06 五阶段，依赖 B2-LLM）
- 全自动扫描 SUT 配置树源码（skill md/脚本/插件）→ LLM 定位插桩入口（新增 instrumentor 模板）→ 结构化插桩计划 → 插桩副本层实施（幂等标记）→ 冒烟验证（事件产出 + 行为不变）→ 用后移除
- 产出插桩覆盖报告（不可插桩环节及原因）；LLM 失败诚实降级回配置级

**4. FR3.4 泛化参考集**
- 泛化集（purpose=generalization）导入/创建入口 + 成绩参考展示（版本对比/看板旁路，标注"仅参考"）
- Selector 显式排除泛化集于晋升判定；新增故障用例引导默认入泛化集

### B6 真机验收轮（E:\rsi_test）

- 前置：precheck.py 体检（设备 SSH×6/孤儿容器/API）、独立环冷却 ≥10min、replay-verify.py 防呆可用、B1 后重启不依赖 provision.py
- 主场景：P1 半自动迭代一轮真实跑通——LLM 精分类归因（真实 GLM）→ 人工门① → LLM 起草预填 → 人工改后提交 → 预审 → 验证 → pairwise 对比 → 看板版本对比曲线呈现
- 演示：构造一类刷分案例被 pairwise 拦截；泛化集参考展示
- 验收四条逐条映射需求 §8 P1 标准并留证；报告写 E:\rsi_test\notes\
- 环境事实约束（记忆沉淀）：设备管理口=GE1/0/10、高频连续配置事务节流（~每 4 段一次 ReadTimeout）、SSH 闲置 ~10min 被掐

## 5. 测试与交付策略

- **文档先行**：每批实现前先落对应文档修订（五文档 + DD 滚动版本）——during_task 契约、脱敏策略、实例池 settings 键、pairwise/traces:cluster 端点等新契约均先进文档
- **离线基线**：1453+12 全绿维持；每批新增 TU/IN 用例 + cassette 回归（attribution/hypothesis/pairwise）
- **批次交付**：沿用项目规程——每批测试先行 → 全量回归零失败 → 独立 commit → `.fN-report.md`；实现类批次串行不并行
- **分支**：`feat/p1-phase` 自 main 切出，批间线性推进，完成合并 `--no-ff` 后推送（CLAUDE.md 已授权标准流程）
- **工作产物清理**：合并前清理 `.audit*/`、`.f*-report.md`、`docs/superpowers/plans/` 未跟踪报告

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| FR5.3 LLM 定位插桩入口质量不确定 | 冒烟验证硬门槛（事件产出+行为不变），失败降级配置级并记覆盖报告 |
| pairwise/归因真机判分受 GLM 方差影响 | 温度 0 + 提示词冻结 + cassette 回归 + 噪声带吸收（ADR-23 护栏沿用） |
| B1 迁移脚本破坏 E:\rsi_test 现网对象 | 迁移只增不改（DB 与内存幂等对账），跑前备份 platform.db |
| zstd 新依赖 | 纯计算库无供应链风险；Windows/Linux 均有 wheel |
| 真机轮设备节流/冷却限制 | 沿用 S11 节拍经验（失败段补试、段间节拍、冷却 ≥10min） |
| 范围大（6 批）周期长 | 批间独立交付，任一批完成即有可合并增量；真机验收不依赖全部批次完美 |

## 7. 验收标准映射

| 需求 §8 P1 验收条 | 交付物 | 批次 |
|---|---|---|
| LLM 归因精分类与假设起草 + cassette 回归 + 质量对比 P0 基线 | FR6.2/6.3/6.9 实现 + 三组件 cassette 测试 + 真机对比度量 | B2 + B6 |
| 噪声带标定与显著性 + Selector 统计置信 | 已实现（P0 审计修复），看板呈现补齐 | B4（确认项） |
| pairwise 上线 + 拦截一类刷分演示 | FR7.2 实现 + 刷分构造演示 | B2 + B6 |
| 看板版本间对比曲线 | FR10.4 版本对比视图（真实聚合） | B4 + B6 |
