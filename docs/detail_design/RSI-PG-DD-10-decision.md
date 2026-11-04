# RSI-PG-DD-10 decision —— 迭代决策

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-10 |
| 版本 | v1.0 |
| 上游 | 设计 3.9；FR9、ADR-17；ADR-21（Selector P0 辅助人工） |
| 交付切片 | S6b（P0 随 optimizer+IterationEngine 交付） |

## 1. 追溯与范围

**职责**：批次指标聚合（StatsAggregator）→ 噪声带标定（NoiseBandCalibrator）→ 加权综合分 → 规则序 0–6 判定（RegularizedSelector）→ 决议记录（DecisionRecorder）。**只判定不执行**（晋升/回退由 IterationEngine 调 versioning）。

**不做什么**：不执行 promote/rollback（versioning 职责）；不做探索模式（ExplorationController P2 接口预留）。

## 2. 文件与类清单

```
rsi/decision/__init__.py
rsi/decision/aggregator.py    # StatsAggregator（BatchStats）
rsi/decision/noise.py         # NoiseBandCalibrator（ADR-17 显式标定）
rsi/decision/significance.py  # SignificanceTester（任务配对检验）
rsi/decision/selector.py      # RegularizedSelector（规则序 0–6）
rsi/decision/recorder.py      # DecisionRecorder（experiments+audit）
rsi/decision/exploration.py   # ExplorationController（P2 接口预留）
```

## 3. 数据结构

（BatchStats/Decision/Efficiency 见 DD-01；NoiseBand 表见接口 §2.2）

```python
class DecisionCtx(BaseModel):          # Selector 唯一输入（接口 §12）
    base_stats: BatchStats; cand_stats: BatchStats
    noise: NoiseBandInfo | None        # 未标定=None → 规则 3 退化为 δ=0
    goals: list[Goal]; guards: GuardPolicy
    cost: CostDelta                    # {Δtokens, Δtool_calls, Δwall_time_s}
    hack_or_fatal: bool                # 来自 Verdict 聚合
    recent_experiments: list[dict]     # ADVISE_MANUAL 附历史对比

class NoiseBandInfo(BaseModel):
    metric: str; sigma: float; delta: float
    k: int; evalset_hash: str; low_confidence: bool; valid: bool

class CostDelta(BaseModel):
    d_tokens: int; d_tool_calls: int; d_wall_time_s: float

class SignificanceResult(BaseModel):
    p_value: float; significant: bool; method: Literal["wilcoxon", "paired_t"]
```

## 4. 对外接口

```python
class StatsAggregator:
    def __init__(self, db_session): ...
    async def aggregate(self, batch_id: str) -> BatchStats
        """从 judgments+runs 聚合：逐指标 mean（缺试计 0 保留分母 missing/n）；
        tokens/tool_calls/wall_time_s 合计（Efficiency 逐 run 累加）；写 batches.stats_json"""
    async def recalc(self, batch_id: str) -> BatchStats   # 重判后重算；标记 experiments.stale

class NoiseBandCalibrator:              # ADR-17
    def __init__(self, batch_runner, db_session): ...
    async def calibrate(self, sut_id: str, evalset_id: str, k: int = 3) -> list[NoiseBandInfo]
        """编排 batches.purpose='noise_calibration'（bypass_cache）：
        分层抽样（按 task_id 域前缀分层，总量 min(10, 任务总数)）→ 复用 BatchRunner 跑 k 次
        → 逐指标 σ → δ=factor×σ（1.96）→ 写 noise_bands 表
        有效任务 < 抽样数/2 → low_confidence=True"""

class SignificanceTester:               # FR9.2 P1
    def test(self, base: list[float], cand: list[float], alpha: float = 0.05) -> SignificanceResult
        """任务配对（缺试按 0 参与配对）；n<30→Wilcoxon signed-rank；n≥30→paired t-test（scipy.stats）"""

class RegularizedSelector:              # FR9.3 P0
    def decide(self, ctx: DecisionCtx) -> Decision
        """主指标=加权综合分 S=Σ wᵢ·(Δmᵢ/σ_base)（max 正/min 负）
        规则序 0–6 按序短路（§5.1）；返回 Decision{action, rule_hit, reason, evidence}"""

class DecisionRecorder:                 # FR9.5 P0
    def __init__(self, db_session, audit: AuditLog): ...
    async def record(self, experiment_id: str, decision: Decision,
                     baseline_batch_id: str, candidate_batch_id: str) -> None
        """experiments.decision/decision_reason/evidence_json 更新 + audit"""
```

## 5. 内部逻辑

### 5.1 RegularizedSelector 规则序（接口 §12，按序短路）

```
0. 候选批缺试率 = cand_stats.missing / cand_stats.n > 0.30 → ADVISE_MANUAL
   reason="缺试率 {:.0%}，环境异常不影响判定"——防误杀
1. ctx.hack_or_fatal == True → REJECT（rule_hit=1）
2. 非补偿守卫：guards 中任一项恶化（missed_check_worsen/safety_any_worsen/high_risk_worsen
   — 比较 base/cand 的 missed_check、violations、high_risk_actions）→ REJECT（rule_hit=2）
3. 加权综合分 S ≤ δ（噪声带内；未标定 δ=0 → 任何非正差异均 ≤0）→ REVERT(tie)（rule_hit=3）
4. 成本规则：S_gain < cost_penalty × (|Δtokens|/base_tokens + |Δcalls|/base_calls + |Δtime|/base_time)
   → REJECT（rule_hit=4）——防"以成本换分"
5. 显著性检验 p < alpha → PROMOTE（P2 前置条件=噪声带已标定，否则跳至 6）（rule_hit=5）
6. 其余 → ADVISE_MANUAL（附 recent_experiments 对比）（rule_hit=6）
```

### 5.2 加权综合分计算

```
S = Σᵢ wᵢ × (Δmᵢ / σ_base,i)
  Δmᵢ = (cand.metrics[mᵢ] - base.metrics[mᵢ])
  σ_base,i = noise.noise_band(mᵢ).sigma if noise else 1.0（未标定退化）
  direction=min 的指标取负 Δ（统一为"越大越好"）
```

### 5.3 与相邻模块协作流

```
BatchRunner（基线批+候选批完成）→ StatsAggregator×2 → DecisionCtx 组装
  → Selector.decide() → Decision
  → IterationEngine：调 versioning.promote / rollback + 回算 predicted_hit_rate 写 hypotheses
  → DecisionRecorder.record()
重判联动：POST /runs/{id}:re-judge → StatsAggregator.recalc(batch) → experiments.stale=True
```

## 6. 依赖矩阵

- imports：scipy.stats、rsi/core、rsi/db。
- 注入：NoiseBandCalibrator←BatchRunner（复用编排）；Selector 无外部依赖（纯函数）。
- DB：**写** batches.stats_json、experiments、noise_bands、hypotheses（hit_rate 回填——由 IterationEngine 执行非本模块直写）；**读** judgments、runs。
- 被依赖：orchestrator/IterationEngine（DD-12 DECIDING 状态消费 Selector）、api（DD-13 noise-bands 端点）。

## 7. 测试映射与实施注记

**测试**：TU-014（规则序 0–6 逐条短路：构造各规则命中场景+边界——缺试率 29%/31%、δ 边界=、成本规则比值边界）、TU-015（合成方差→δ 计算正确+配对检验 alpha 生效：wilcoxon vs paired_t 切换）、IN-003（缓存/重算/stale 标记）。

**实施注记**：① StatsAggregator 聚合用 SQL GROUP BY（judgments JOIN runs WHERE batch_id=?），逐指标 AVG+COUNT；② NoiseBandCalibrator 的分层抽样：按 task_id.split('/')[0]（域前缀）分组 → 每组均匀取样 → 合并至 min(10, total)；③ Selector 为纯函数（无 IO），决策上下文由调用方组装——天然可单测；④ ADR-17 约束：P2 自动晋升前置条件=`noise is not None and noise.valid`；⑤ DecisionRecorder 同步写 experiments+audit（一个事务）。
