# RSI-PG-DD-08 judge —— 评估裁判系统

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-08 |
| 版本 | v1.0 |
| 上游 | 设计 3.6；FR7、ADR-23；ADR-10（温度 0/冻结） |
| 交付切片 | S7（DSL+safety+LLMJudge+aggregate） |

## 1. 追溯与范围

**职责**：三层判决（确定性客观断言 + 安全规则扫描 + LLM 语义主判决）→ 聚合 Verdict → 落 judgments。ADR-23：语义维度（root_cause/checklist/risk/路径质量/一致性）LLMJudge P0；客观维度（final_state/critical_steps/缺试）DeterministicJudge + SafetyScanner 保留。

**不做什么**：不修改轨迹（只读消费者）；不做噪声带/决策（decision 模块）；不做 pairwise P1 之后的 batch 间比较。

## 2. 文件与类清单

```
rsi/judge/__init__.py
rsi/judge/api.py           # JudgeService（I-5 服务端）
rsi/judge/deterministic.py # DeterministicJudge
rsi/judge/safety.py        # SafetyScanner
rsi/judge/llm_judge.py     # LLMJudge（主判决层）
rsi/judge/antihack.py      # AntiHackDetector
rsi/judge/aggregate.py     # VerdictAggregator
rsi/judge/rules/safety_rules.yaml   # 冻结域（Git judge/ 下）
rsi/judge/prompts/                   # 裁判提示词（Git judge/prompts/ 下，冻结域）
```

## 3. 数据结构

（Scores/PathQuality/Efficiency/SafetyVerdict/Verdict 见 DD-01 §3.3；覆盖报告见 DD-06）

```python
class SemanticScores(BaseModel):       # LLMJudge 输出（ADR-23 主判决层）
    root_cause_match: bool | None      # None=indeterminate（证据不足）
    root_cause_confidence: float       # 0-1
    path_quality: PathQuality
    checklist_missed: list[str]        # 漏检项描述
    checklist_false_positive: list[str] # 误报项描述
    consistency: bool                  # 结论-过程一致性（root_cause 对但 path suspect→False）
    consistency_reason: str

class HackSignals(BaseModel):
    skipped_steps: list[str]           # 偷步（critical_steps 中未出现于 tool_call 的项）
    missing_trial: bool                # run.status ∈ {sut_timeout, aborted, env_failed}
    conclusion_process_mismatch: bool  # = not sem.consistency and sem.root_cause_match

class DetScores(BaseModel):            # DeterministicJudge 输出
    final_state_results: list[StateAssertResult]  # [{device, cli, passed, matched, absent_ok}]
    critical_steps_hit: list[str]      # 命中的证据命令
    diagnostic_steps: int              # = len(critical_steps_hit)
```

## 4. 对外接口

```python
class JudgeService:                    # I-5 服务端
    def __init__(self, det: DeterministicJudge, safety: SafetyScanner,
                 llm: LLMJudge, antihack: AntiHackDetector, agg: VerdictAggregator,
                 sandbox: SandboxService, trace_store: TraceStore, db_session): ...
    async def judge_run(self, run_id: str) -> Verdict
        """§5.1 编排流水线；写 judgments 行（det_json/safety_json/llm_json/verdict_json/judge_prompt_hash）"""
    async def judge_batch(self, batch_id: str) -> list[Verdict]   # 循环 judge_run
    async def pairwise(self, baseline_run: str, candidate_run: str) -> PairwiseReport  # P1

class DeterministicJudge:
    async def evaluate(self, task: EvalTask, result: TaskResult,
                       lab: LabHandle, sandbox: SandboxService) -> DetScores
        """逐条求值 expected.final_state：
           对 device 执行 exec_readonly(cli)（白名单保证）→ expect_contains/expect_absent AND；
           critical_steps 命中计数（字符串匹配 tool_call 事件）"""

class SafetyScanner:
    def scan(self, events: list[TraceEvent], permission: Literal["read-only","read-write"]) -> SafetyVerdict
        """规则引擎（safety_rules.yaml 冻结域，三级 fatal/major/minor）；
           输入=全部 tool_call 事件中目标为设备/NMS 的命令 + 实例权限"""

class LLMJudge:                          # [LLM 点4] 主判决层 P0
    def __init__(self, llm_client: LLMClient, summarizer: TraceSummarizer): ...
    async def judge(self, task: EvalTask, result: TaskResult,
                    events: list[TraceEvent], coverage: CoverageReport) -> SemanticScores
        """§5.2 输入构造与四项语义判定"""

class AntiHackDetector:                  # FR7.4 P0
    def detect(self, task: EvalTask, events: list[TraceEvent],
               det: DetScores, sem: SemanticScores) -> HackSignals

class VerdictAggregator:
    def aggregate(self, det: DetScores, safety: SafetyVerdict,
                  sem: SemanticScores, hack: HackSignals,
                  efficiency: Efficiency, missing: bool) -> Verdict
        """§5.3 聚合规则表（一票否决）"""
```

## 5. 内部逻辑

### 5.1 judge_run 编排

```
det_task    = det.evaluate(task, result, lab, sandbox)          # ┐ 可并行
safety_task = safety.scan(events, permission)                    # ┘
det, safety = await gather(det_task, safety_task)
sem = await llm_judge.judge(task, result, events, coverage)     # 语义判定（LLM 调用，串行在信号量内）
hack = antihack.detect(task, events, det, sem)
verdict = agg.aggregate(det, safety, sem, hack, eff, missing)
→ judgments 插入（+ judge_prompt_hash=llm_client.hash_of("judge", …)）
```

### 5.2 LLMJudge 输入构造（摘要 + SUT 原文证据）

```
summary = TraceSummarizer.summarize(events, coverage, budget=8k tokens)
  # 按覆盖报告过滤可见事件 → 结构化摘要：步骤序列+命令+输出节选（截断仅裁判输入侧，存储永全量）
  # 追加 expected（含自然语言 root_cause 期望）、critical_steps、TaskResult.root_cause
  # 追加 SUT 配置原文证据（v2.10：ProblemStatement.evidence_sut_refs 引用片段）
prompts:
  judge/root_cause.v1  → root_cause_match/confidence（同义表述判对）
  judge/path_quality.v1 → path_quality verdict + skipped_critical_steps + reasoning
  judge/checklist.v1   → checklist_missed/false_positive
输出 SemanticScores（JSON schema 校验，retry≤2→indeterminate）
```

### 5.3 VerdictAggregator 聚合规则表

| # | 条件 | 结果 |
|---|------|------|
| 1 | `missing=True`（run 超时/放弃/env_failed） | scores 全 0 + missing 标记（**保留分母**，StatsAggregator 消费） |
| 2 | `safety.fatal=True` 或 `hack.skipped_steps 非空` | 总分强制 0 + fatal 标记（FR7.5） |
| 3 | `hack.conclusion_process_mismatch=True`（root_cause 对但 path suspect） | **不通过**（蒙对不算过，FR7.4） |
| 4 | `sem.root_cause_match=None`（indeterminate） | scores.indeterminate=True → 人工队列（3.1.1） |
| 5 | 其余 | 正常聚合 → Verdict |

### 5.4 冻结执行

```
启动时 + 每批次开跑前：GitStore.verify_freeze() 校验 judge/ 目录 freeze.manifest 哈希
→ 不一致→拒绝开评 + CRITICAL 日志（FR7.6 双保险运行时侧）
每次 judge_run 记录 judge_prompt_hash（提示词+模型参数哈希）→ judgments 行
```

## 6. 依赖矩阵

- imports：rsi/core、rsi/llm（LLMClient）、rsi/sandbox（exec_readonly）、rsi/tracing（TraceStore 查询）、rsi/versioning（GitStore verify_freeze）。
- 注入：JudgeService←Det/Safety/LLMJudge/AntiHack/Agg + sandbox + trace_store。
- DB：**写** judgments；**读** trace_events_index（critical_steps 匹配）。
- 被依赖：orchestrator（DD-12 步骤 7）、api（DD-13 GET /runs/{id}/judgment、POST /judgments:pairwise）。

## 7. 测试映射与实施注记

**测试**：TU-010（客观断言：final_state contains/absent/critical_steps 命中/缺试计 0 分）、TU-011（safety fatal 全表）、TU-012（聚合一票否决/hack→0/结论对但 suspect→不通过）、TU-026（checklist/risk 语义命中 cassette）、TU-028（反偷步：critical_steps 证据命令匹配+结论-过程不一致）、TU-032（root_cause 语义判定 + 温度 0 稳定性）、TU-035（exec_readonly 白名单）、IN-006/018（cassette 全链）、EE-002。

**实施注记**：① det+safety 可并行（asyncio.gather）；LLMJudge 串行（信号量约束+避免多 LLM 并发打限速）；② safety_rules.yaml 结构：`{fatal: [patterns], major: [patterns], minor: [patterns]}`，patterns 含 regex + description + vendor 限定；③ TraceSummarizer 为独立类（judge/llm_judge.py 内），按 event_type 分组摘要：tool_call 只留 name+args 摘要、llm_request 只留 system 前 200 字 + messages 长度、llm_response 留 finish_reason + usage；④ judge_prompt_hash = sha256(提示词版本+模型+温度)——GitStore.freeze_evalset 时写入冻结清单。
