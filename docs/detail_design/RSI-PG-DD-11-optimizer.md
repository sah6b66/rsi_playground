# RSI-PG-DD-11 optimizer —— 优化引擎

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-11 |
| 版本 | v1.0 |
| 上游 | 设计 3.7；FR6、ADR-16（执行器协议）/ADR-21（P0 规则层+人工门）；ADR-20 |
| 交付切片 | S6b（P0 随 IterationEngine 交付） |

## 1. 追溯与范围

**职责**：六组件——TargetRegistry（路径域）、AttributionAnalyzer（规则层归因 P0 / LLM 精分类 P1）、HypothesisGenerator（人工门 P0 / LLM 起草 P1）、Critic（确定性+人工门 P0 / LLM P2）、ChangeApplier（P0 全）、SftAssessor（P0 模板/P1 半自动）。执行器协议（ADR-16）：HypothesisSource/ReviewGate 可插拔。

**不做什么**：不执行批次（BatchRunner，DD-12）；不做决策（decision，DD-10）；不直接写文件系统（经 GitStore，DD-09）。

## 2. 文件与类清单

```
rsi/optimizer/__init__.py
rsi/optimizer/targets.py       # TargetRegistry
rsi/optimizer/attribution.py   # AttributionAnalyzer [LLM 点1]
rsi/optimizer/hypothesis.py    # HypothesisGenerator [LLM 点2] + Validator
rsi/optimizer/critic.py        # Critic（det 层 + ReviewGate 分发） [LLM 点3 P2]
rsi/optimizer/applier.py       # ChangeApplier
rsi/optimizer/sft_assessor.py  # SftAssessor
rsi/optimizer/executors.py     # WebformHypothesisSource / ManualReviewGate（P0 人工门）
                                 # LLMHypothesisSource / AutoReviewGate（P1 起草 / P2 自动）
```

## 3. 数据结构

（Hypothesis/ProblemStatement/AttributionReport/AttributionCluster 见 DD-01 §3.4；OptimizationProfile 见 §3.5）

```python
class TargetRegistry:
    TARGET_PATHS: dict[str, str] = {       # 优化项 → suts/<id>/ 路径正则
      "system_prompt": r"suts/{id}/system-prompt(\.md)?",
      "skills":       r"suts/{id}/skills/.*",
      "scripts":      r"suts/{id}/scripts/.*",
      "mcp_config":   r"suts/{id}/mcp\.json",
      "mcp_src":      r"suts/{id}/mcp-src/.*",
      "memory":       r"suts/{id}/memory/.*",
      "knowledge":    r"suts/{id}/knowledge/.*",
      "model_config": r"suts/{id}/model\.json",
    }
    FROZEN_PATHS: tuple[str, ...] = (r"judge/.*", r"prompts/.*", r"evalsets/.*")

class CriticVerdict(BaseModel):
    verdict: Literal["pass", "reject", "repair"]
    reasons: list[str]
    det_results: list[str]                 # 确定性检查结论

class SutConfigSnapshot(BaseModel):        # v2.10 归因原文输入
    config_version_id: str
    files: dict[str, str]                  # 路径→内容全文（suspected_elements 相关文件）
```

## 4. 对外接口

```python
class TargetRegistry:
    def validate(self, changeset: Changeset, enabled_targets: set[str]) -> None
        """逐文件路径匹配：① 属于 enabled_targets 之一；② 不在 FROZEN_PATHS 内。
        违规→TargetViolationError(path, target)——FR6.1 执行层校验"""
    def editable_elements(self, sut_id: str, enabled: set[str]) -> list[EditableElement]
        """列出当前可改要素（enabled targets 的文件域清单+内容摘要）→ 假设生成输入"""

class AttributionAnalyzer:             # [LLM 点1] FR6.2
    def __init__(self, trace_store, llm_client, gitstore, db_session): ...
    async def analyze(self, failed_runs: list[str], sut_config: SutConfigSnapshot,
                      opt_history: list[dict]) -> AttributionReport
        """三层流水线（§5.1）：FeatureExtractor（规则）→ PreClusterer（规则）→ LLM 精分类（P1）
        P0 规则层输出粗报告；LLM 层 P1 增强（attribution/classify.v1）
        opt_history（v2.15 P0）：读 experiments+hypotheses 最近 N=10 轮
        [{round, proposal_type, decision, failure_mode, reason}]——
        标注'此簇已尝试 X 未果'，Top-N 排序 ×历史有效性因子"""

class HypothesisGenerator:            # [LLM 点2] FR6.3
    def __init__(self, llm_client, gitstore): ...
    async def stage1_problem_statement(self, cluster: AttributionCluster,
                                        sut_config: SutConfigSnapshot) -> ProblemStatement
        """prompts/hypothesis/diagnose.v1；P0=LLM 起草草稿供人工编辑（Webform 门消费）"""
    async def stage2_propose(self, ps: ProblemStatement, profile: OptimizationProfile,
                             failed_hyp_hashes: list[str]) -> Hypothesis
        """prompts/hypothesis/propose.v1；输入含相关要素原文（suspected_elements 定位文件全文自 GitStore）
        + 可改要素清单（仅 enabled_targets）+ 失败假设摘要；输出 Hypothesis"""

class Critic:                          # [LLM 点3] FR6.10
    def __init__(self, registry: TargetRegistry, review_gate: ReviewGate): ...
    async def review(self, changeset: Changeset, profile: OptimizationProfile) -> CriticVerdict
        """双层：① DeterministicCritic（§5.2 确定性检查）→ 任一不过→reject+原因码
                 ② review_gate.review(changeset, det_results) → 人工/P2 LLM 裁决
        RepairLoop 由 IterationEngine 驱动（≤ critic_max_repair_rounds=5 轮）"""

class ChangeApplier:                   # FR6.4/6.5
    def __init__(self, gitstore: GitStore, registry: TargetRegistry): ...
    async def apply(self, sut_id: str, hyp: Hypothesis, profile: OptimizationProfile) -> VersionId
        """① gitstore.snapshot(sut_id)（FR6.5 强制，未快照→raise）
        ② 产生 Changeset（diff_content→文件级操作 / model_config_patch→改 model.json）
        ③ registry.validate（双重校验与 GitStore 侧独立）
        ④ gitstore.commit_candidate → candidate_version_id"""

class SftAssessor:                     # FR6.9
    def __init__(self, db_session, llm_client): ...
    async def assess(self, sut_id: str, defect_b_clusters: list[AttributionCluster],
                     opt_history: list[dict]) -> SftReport | None
        """AdmissionChecker（§5.3 规则）→ ReportWriter（P0 模板人工填 / P1 LLM 起草）"""

# ── 执行器协议（ADR-16）──
class HypothesisSource(Protocol):
    async def generate(self, ps: ProblemStatement, profile: OptimizationProfile,
                       failed_hyp_hashes: list[str]) -> Hypothesis
class WebformHypothesisSource:        # P0：WEB 表单/文件导入 → Hypothesis（人工门② API 入口）
    async def generate(…) -> Hypothesis   # 直接返回人工提交的 Hypothesis（已经 API 校验）
class LLMHypothesisSource:            # P1：LLM 起草+人工修订 / P2：全自动
    async def generate(…) -> Hypothesis   # 调 hypothesis.stage1+stage2

class ReviewGate(Protocol):
    async def review(self, changeset: Changeset, det_results: list[str]) -> CriticVerdict
class ManualReviewGate:               # P0：人工预审表单（人工门③ API 入口）
    async def review(…) -> CriticVerdict  # 返回人工裁决结果
class AutoReviewGate:                 # P2：LLMCritic（critic/review.v1 六类审查）
    async def review(…) -> CriticVerdict  # 调 LLM + 六类审查清单
```

## 5. 内部逻辑

### 5.1 AttributionAnalyzer 三层流水线

```
Layer 1 FeatureExtractor（规则，P0）：
  for run in failed_runs:
    events = trace_store.query(run_id=run)
    features = {
      cli_commands: [tool_call.data.args.command …],      # 命令序列
      error_hits: [tool_result for tool_result if not ok], # 报错
      critical_steps_hit: det.critical_steps_hit,           # 判决结果
      final_json_parsed: not result.parse_error,
      knowledge_queries: [knowledge_query.data.query …],   # 知识检索
    }
    # 对照 SUT 原文（v2.10）：
    for step in expected.critical_steps:
      if step.evidence not in features.cli_commands:
        if step.evidence in sut_config.files.get("system-prompt.md", ""): → "指引已有未执行"（A 类）
        else: → "指引缺失"（优化方向=补提示词/知识库）

Layer 2 PreClusterer（规则，P0）：
  按失败签名（critical_steps 命中差集 + error_hits 交集）贪心聚类
  簇 ≥2 保留独立簇；散点归"杂项"簇
  ★ 历史×有效性排序（v2.15）：
  for cluster in clusters:
    cluster.opt_history_match = match_against(opt_history, cluster.failure_mode)
    # match: 该失败模式在 opt_history 中是否出现过（含提案类型与结果）
    # 排序因子 = 簇大小 × 可优化性 × history_factor
    #   history_factor: 无历史=1.0; 历史全部失败=0.3(降权); 有成功=1.2(升权)
    #   → 已反复尝试同方向且全败的簇自动下沉，新鲜方向或已证明有效的方向上浮

Layer 3 LLM 精分类（P1）：
  prompts/attribution/classify.v1：
  输入=簇样本轨迹摘要 + 相关要素原文 + opt_history 上下文（该簇的历史尝试及结果）
  输出=失败模式标签 + A/B 标注 + 原文证据引用 + 历史尝试标注（"此簇 round 3 已尝试 modify_prompt 未果"）
```

### 5.2 DeterministicCritic 检查项

```
1. 路径越界：changeset 中任何文件不匹配 TARGET_PATHS 任何项 → reject "path_out_of_domain"
2. 冻结域：匹配 FROZEN_PATHS → reject "frozen_domain"
3. 超 enabled_targets：文件匹配的 target 不在 profile.enabled_targets → reject "target_not_enabled"
4. 声明与实际不符：changeset.files[].target 与路径匹配结果不一致 → reject "declaration_mismatch"
5. P2 单点约束：changeset 涉及 >1 个 target → reject "multi_target"（P1 仅告警）
```

### 5.3 SftAssessor AdmissionChecker

```
条件（全部满足才继续）：
  ① 同缺陷类连续无收益轮数 ≥ optimizer.sft.no_gain_rounds（默认 5）
     （读 experiments：该 sut_id 的最近 N 轮 decision ∈ {reverted, advise_manual}）
  ② 失败轨迹样本量 ≥ optimizer.sft.min_samples（默认 50）
     （读 runs：failed status 的 run 数）
  ③ 知识/skill 已尝试标记（读 hypotheses 历史：该缺陷类存在 add_knowledge 或 skills 类提案）
不满足→ return None
满足→ ReportWriter 生成 sft-<exp_id>.md（P0=Jinja2 模板人工填写/P1=LLM 起草）
```

## 6. 依赖矩阵

- imports：rsi/core、rsi/llm、rsi/versioning（GitStore）、rsi/tracing（TraceStore）、rsi/db。
- 注入：各组件←LLMClient+GitStore+TraceStore+db_session；Critic←TargetRegistry+ReviewGate。
- DB：**写** hypotheses、experiments（经 DecisionRecorder）；**读** runs、judgments（归因）、experiments（SFT 准入）。
- 被依赖：orchestrator/IterationEngine（DD-12 ATTRIBUTING/HYPOTHESIZING/REVIEWING/APPLYING 状态消费）、api（DD-13 hypotheses/iterations 门端点）。

## 7. 测试映射与实施注记

**测试**：TU-013（TargetRegistry 八项正反例+冻结域越界）、TU-020（Critic det 层：越界/冻结/超 targets/声明不符）、TU-033（归因原文对照：提示词有/无→不同失败模式+B 类证据引用+token 裁剪+**opt_history 排序因子**：历史全败簇降权/有成功簇升权）、IN-006（优化迭代 cassette 全线）、IN-007（SFT 准入 N 轮无收益+样本量+尝试标记）。

**实施注记**：① AttributionAnalyzer P0 规则层无 LLM 调用（FeatureExtractor+PreClusterer 纯代码）；LLM 层 P1 时才注入 LLMClient；② stage1/stage2 在 P0 均为“LLM 起草草稿→人工编辑确认”模式（WebformHypothesisSource 消费 LLM 草稿或人工从零填写）；③ RepairLoop：Critic 返回 reject→IterationEngine 调 HypothesisGenerator 重新生成（携 reasons）→再次 Critic→循环 ≤5 轮→仍拒转人工队列；④ SftReport 文件落 `var/artifacts/<batch_id>/reports/sft-<exp_id>.md`；⑤ EditableElement={target, path, summary(前 200 字)}，供 WEB 表单展示。
