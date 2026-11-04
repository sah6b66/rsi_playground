# RSI-PG-DD-12 orchestrator —— 编排引擎

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-12 |
| 版本 | v1.2（H1-H8 全外部依赖监控：HealthMonitor/8 类检查器/依赖状态 API/SSE 告警） |
| 上游 | 设计 3.1；FR5、ADR-8/21/24；ADR-14/17/20 关联 |
| 交付切片 | S6（batch+checkpoint+budget）+ S6b（IterationEngine+三级编排） |

## 1. 追溯与范围

**职责**：BatchRunner（评测批次状态机：构建→就绪→逐任务执行→判决→聚合→完成）+ IterationEngine（优化迭代状态机：归因→假设→预审→应用→三级验证→决策）+ BudgetController（三级预算）+ CheckpointStore（断点续跑）+ EventBus（SSE 事件源）。

**不做什么**：不直接操作设备（sandbox 职责）；不直接管理容器（carrier 职责）；不做判定（judge 职责）；不做决策规则（decision 职责）——orchestrator 是纯编排/调度/状态管理。

## 2. 文件与类清单

```
rsi/orchestrator/__init__.py
rsi/orchestrator/batch.py        # BatchRunner + EventBus
rsi/orchestrator/iteration.py    # IterationEngine
rsi/orchestrator/budget.py       # BudgetController（三级预算+全局）
rsi/orchestrator/checkpoint.py   # CheckpointStore
rsi/orchestrator/scheduler.py    # LabScheduler（沙箱实例选择，ADR-24）
```

## 3. 数据结构

（BatchStatus/RunStatus/IterationState 见 DD-01 §3.11；Decision 见 DD-01 §3.9）

```python
class BatchStats(BaseModel):         # 见 DD-01
    metrics: dict[str, float]; n: int; missing: int
    tokens: int; tool_calls: int; wall_time_s: float; tokens_estimated: bool

class BudgetSnapshot(BaseModel):
    scope: str                       # batch_id | iteration_id | "__global__"
    tokens_used: int; cost_usd: float; wall_time_s: float
    tokens_limit: int; cost_limit: float | None; wall_limit_s: float | None

class CheckpointState(BaseModel):
    batch_id: str; evalset_id: str; lab_id: str; snap0_id: str
    completed: list[CompletedTask]   # [{task_id, run_id, status}]
    cursor_seq: int
    budget_snapshot: BudgetSnapshot

class EventBusMessage(BaseModel):    # SSE 信封（接口 §9.20）
    event: str                       # "<object>.status_changed"
    ts: str
    data: dict                       # {object_id, from, to}

class LabSelection(BaseModel):       # ADR-24 LabScheduler 输出
    backend_key: str                 # 选中的沙箱实例配置键
    reason: str                      # "explicit" | "least_active" | "only_available"
```

## 4. 对外接口

### 4.1 BatchRunner（接口 §11）

```python
class BatchRunner:
    def __init__(self, sandbox: SandboxService, carrier: HostService,
                 instrumentor: Instrumentor, collector: TraceCollector,
                 judge: JudgeService, ckpt: CheckpointStore, budget: BudgetController,
                 stats_agg: StatsAggregator, event_bus: EventBus, settings): ...
    # Instrumentor = 五阶段门面（DD-06）；collector = TraceCollector（DD-07）

    async def run_batch(self, batch_id: str) -> BatchStats:
        """批次主循环（§5.1）"""

    async def run_task(self, batch_id: str, task: EvalTask, seq: int) -> RunResult:
        """单任务九步流水线（§5.2）"""

    async def abort_batch(self, batch_id: str) -> None     # POST /batches/{id}:abort
    async def pause_batch(self, batch_id: str) -> None     # :pause → PAUSED 标记
    async def resume_batch(self, batch_id: str) -> None    # :resume → 从 checkpoint 继续
    async def rerun(self, batch_id: str, scope: str, bypass_cache: bool) -> int  # :rerun 重测
```

### 4.2 IterationEngine

```python
class IterationEngine:
    def __init__(self, batch_runner, optimizer: dict,  # {attribution, hypothesis, critic, applier}
                 decision: dict,                        # {selector, recorder, noise}
                 gitstore: GitStore, profile: OptimizationProfile,
                 event_bus: EventBus, budget: BudgetController): ...

    async def iterate_once(self, sut_id: str, profile: OptimizationProfile,
                           mode: Literal["p1", "p2"] = "p1") -> ExperimentRecord:
        """一轮迭代（接口 §11）；状态机 §5.3；人工门等待 §5.4"""

    async def start(self, iteration_id: str) -> None     # POST /iterations
    async def pause(self, iteration_id: str) -> None     # :pause → PAUSED
    async def resume(self, iteration_id: str) -> None    # :resume
    async def abort(self, iteration_id: str) -> None     # :abort
    async def archive(self, iteration_id: str) -> None   # DELETE → ARCHIVED（软删）

    # ── 人工门 API（P0/P1 模式，四门端点的服务端）──
    async def gate_confirm_direction(self, iteration_id: str, selected: list[str]) -> None
    async def gate_hypothesis(self, iteration_id: str, hyp: Hypothesis) -> str   # → hyp_id
    async def gate_review(self, iteration_id: str, verdict: dict) -> None
    async def gate_decision(self, iteration_id: str, action: str, reason: str) -> None
```

### 4.3 BudgetController（三级）

```python
class BudgetController:
    def __init__(self, db_session, stop_file: Path, settings): ...
    async def check_and_reserve(self, scope: str, est: BudgetSnapshot) -> None
        """scope 预算：tokens_used + est.tokens > tokens_limit → BudgetExceeded(429)"""
    async def record(self, scope: str, actual: dict) -> None
        """llm_calls 实时累加→内存缓存 + 定期刷 DB"""
    async def check_global(self) -> None
        """全局预算：SUM(全部活跃 scope) > global_limit → 置 STOP 文件 + BudgetExceeded（全局熔断）"""
    async def halt(self, scope: str) -> None
        """scope 级 halt：标记该批次 aborted（不影响其他批次）"""
    def check_stop_file(self) -> bool                  # var/STOP 存在？
    async def start_stop_polling(self, interval_s: int = 5) -> asyncio.Task
```

### 4.4 CheckpointStore

```python
class CheckpointStore:
    def __init__(self, db_session): ...
    async def save(self, scope: str, state: CheckpointState) -> None
        """checkpoints 表 upsert；受 checkpoint_interval_s 最小间隔合并写"""
    async def resume(self, scope: str) -> CheckpointState | None
```

### 4.5 EventBus

```python
class EventBus:
    def __init__(self): ...
    async def publish(self, object_type: str, object_id: str, from_: str, to: str) -> None
        """构造 EventBusMessage → fan-out 到全部订阅者（有界队列 256，满则丢旧保新）"""
    async def publish_alert(self, severity: Literal["info","warning","critical"],
                            message: str, scope: str | None = None) -> None
        """F4 v1.1：system.alert 事件——SSE 推送到前端 toast/红色徽章
        {event: "system.alert", ts, data: {severity, message, scope}}"""
    async def subscribe(self) -> asyncio.Queue
        """SSE 端点（DD-13）调用；yield EventBusMessage"""
    def unsubscribe(self, queue: asyncio.Queue) -> None
```

### 4.6 LabScheduler（ADR-24）

```python
class LabScheduler:
    def __init__(self, sandbox_service, db_session): ...
    async def select(self, env: EnvSpec, explicit_key: str | None = None) -> LabSelection
        """explicit_key 非空→直接返回；
        否则查同类型全部后端实例→最少活跃 lab 的实例→返回 LabSelection
        全部实例忙→排队等待（asyncio.Condition）"""
```

### 4.7 HealthMonitor（v1.2 H1-H8 全外部依赖监控）

```python
class HealthMonitor:
    """后台 asyncio.Task，每 30s 轮询（可配 orchestrator.health_monitor_interval_s）
    8 类检查器并发执行（asyncio.gather），结果汇总 → 内存缓存 + DB 更新 + SSE 告警"""

    def __init__(self, host_pool, ssh_pool, docker_clients, llm_providers,
                 git_repo_path, db_url, trace_collector, evalset_config_apis,
                 event_bus, db_session): ...

    async def start(self) -> asyncio.Task          # 随 platform-api lifespan 启动
    async def check_all(self) -> list[DependencyStatus]  # 8 类并发检查

    # ── 8 类检查器（各自 async，超时 5s，结果缓存）──
    async def check_docker_daemons(self) -> list[DependencyStatus]
        # HostPool.hosts 全部键 → docker version()（to_thread）
        # → status: up（成功）/ down（连接失败）/ degraded（响应慢 >3s）

    async def check_lab_devices(self) -> list[DependencyStatus]
        # labs WHERE status IN (building, ready) → 每 lab 逐设备 TCP connect(ip, port, timeout=3)
        # online = 任一设备可达（用户定义）；devices_online = f"{reachable}/{total}"
        # → 写 labs.online + labs.devices_online
        # → 状态翻转（online True↔False）→ publish_alert

    async def check_sut_instances(self) -> list[DependencyStatus]
        # instances WHERE status='RUNNING' → docker inspect → 实际 status
        # → down: 容器 exited/异常退出；up: running；degraded: paused/restarting

    async def check_llm_providers(self) -> list[DependencyStatus]
        # llm.providers 全部键 → HTTP HEAD/GET base_url（超时 5s）
        # 记录 latency_ms + 最近 10 次调用的错误率（从 llm_calls 表读）
        # → degraded: 错误率 >50% 或延迟 >30s；down: 连接超时

    async def check_git_repo(self) -> DependencyStatus
        # git fsck --no-progress（快速完整性检查）→ up/down
        # shutil.disk_usage(repo_path) → degraded: 磁盘使用率 >85%

    async def check_database(self) -> DependencyStatus
        # SELECT 1 → up/down；disk_usage(db_path) → degraded: >85%

    async def check_collector(self) -> DependencyStatus
        # TraceCollector 内部 Task 存活检查 + 待写队列深度
        # → degraded: 队列 >1000（积压）；down: Task 不存活

    async def check_config_apis(self) -> list[DependencyStatus]
        # 全部 evalsets 的 config_apis 去重 → HTTP HEAD（超时 5s）
        # → up/down

    def get_cached(self) -> list[DependencyStatus]    # GET /system/dependencies 数据源
```

```python
class DependencyStatus(BaseModel):
    key: str        # 唯一键：如 "docker-main" / "lab-<id>" / "llm-anthropic" / "git-repo" / "db" / "collector" / "config-api-<name>"
    kind: Literal["docker", "lab", "llm", "git", "db", "collector", "config_api", "nms"]
    status: Literal["up", "down", "degraded"]
    detail: str     # 人可读："2/3 devices online" / "rate limited" / "disk 85%"
    latency_ms: int
    last_checked: str  # ISO timestamp
```

## 5. 内部逻辑

### 5.1 run_batch 主循环（v1.1 含 F1 连续失败熔断）

```
async def run_batch(batch_id):
    batch = load_batch(batch_id)
    if ckpt := resume(batch_id):     # 断点恢复
        if labs[ckpt.lab_id].status == "ready": reuse_lab = ckpt.lab_id
        else: rebuild_lab()
    else:
        # 首次启动
        selection = lab_scheduler.select(env, batch.backend_key)
        lab = sandbox.build_lab(env, selection.backend_key)
        sandbox.wait_ready(lab)
        snap0 = sandbox.snapshot(lab, name="snap0")

    consecutive_env_failures = 0     # F1 连续失败计数器

    for task in eval_tasks:
        if stop_file_exists(): → batch.status = ABORTED; return
        if task.task_id in ckpt.completed: continue    # 跳过已完成

        # F3 任务前健康探测（v1.1）
        healthy = await sandbox.health_check(lab, timeout_s=5)
        if not healthy:
            health_fail_count += 1
            if health_fail_count >= 2:
                # 判定沙箱离线 → F1 熔断
                batch.status = FAILED; batch.reason = "sandbox_offline"
                lab.status = ERROR
                event_bus.publish_alert("critical", f"沙箱 {lab.lab_id} 离线，批次 {batch_id} 中止")
                return
            continue  # 跳过本任务，下次再探测
        else:
            health_fail_count = 0

        result = await run_task(batch_id, task, seq)

        # F1 连续 env_failed 熔断
        if result.status == "env_failed":
            consecutive_env_failures += 1
            if consecutive_env_failures >= 3:
                batch.status = FAILED; batch.reason = "consecutive_env_failures"
                lab.status = ERROR
                event_bus.publish_alert("critical",
                    f"连续 {consecutive_env_failures} 个任务环境失败，沙箱可能离线，批次中止")
                return
        else:
            consecutive_env_failures = 0

        event_bus.publish("run", result.run_id, "…", result.status)
        ckpt.save(batch_id, updated_state)
        budget.record(batch_id, result.usage)

    stats = stats_agg.aggregate(batch_id)
    batch.stats_json = stats; batch.status = FINALIZED
```
```

### 5.2 run_task 九步流水线

```
async def run_task(batch_id, task, seq):
    # 1. 缓存查询（ADR-5 键）
    cache_key = (config_sha, task_hash, env_hash, model_sha, backend)
    if cached := db.runs.find(cache_key=cache_key):
        → status='cached'; return cached_verdict

    # 2. 数据集级（仅批次首个任务——已在 run_batch 中完成 build/snap0）

    # 3. prepare 评测准备（ADR-20 两种手段）
    try:
        applied = sandbox.prepare(lab, task.task_id, task.prepare.prepare)
    except EnvError:
        retry ≤2: restore_task → prepare → 仍失败→ env_failed(0 分)→ continue

    # 4. 创建 SUT 实例（P0 每 run 一实例）
    instance = carrier.create_instance(
        image_ref, config_version_id, lab,
        permission=batch.permission, instrument=batch.instrument,
        run_id=run_id)

    # 5. 执行任务（超时→abort→sut_timeout；容器崩溃→instance_crashed 区分，F2 v1.1）
    handle = carrier.submit_task(instance, task.task)
    result = await carrier.collect(handle, timeout_s=carrier.task_timeout_s)
    # collect() 内部检查容器 exit_code：
    #   exit_code 非零（137=OOM / 139=segfault / 1=error）→ result.instance_crashed=True
    #     → 不计入归因样本（infra 故障非 Agent 缺陷），计入 infra 统计
    #   exit_code == 0 但 stdout 空/无 FINAL_JSON → result.parse_error=True（A 类归因样本）

    # 6. 轨迹收集
    await collector.finalize(run_id)

    # 7. 判决（三层，DD-08）
    verdict = await judge.judge_run(run_id)

    # 8. 任务后恢复（ADR-20 L1，判决后——终态断言需故障态在场）
    try:
        sandbox.restore_task(lab, applied)
    except EnvError:
        retry ≤2 → 仍失败→ L2 环境级恢复（reset+重放 ops.jsonl）

    # 9. 断点保存
    ckpt.append_completed(batch_id, task.task_id, run_id, verdict.status)
    return result
```

### 5.3 IterationEngine 状态机

```
async def iterate_once(sut_id, profile, mode):
    exp_id = new_id("exp")

    # ATTRIBUTING
    report = attribution.analyze(failed_runs, sut_config, opt_history)
    if mode == "p1": → 等待人工门①（gate_confirm_direction）
    clusters = selected_clusters

    for cluster in clusters:
        # HYPOTHESIZING
        ps = hypothesis.stage1(cluster, sut_config)
        hyp = hypothesis_source.generate(ps, profile, failed_hashes)  # 人工 or LLM
        if mode == "p1": → 等待人工门②（gate_hypothesis）

        # REVIEWING
        changeset = build_changeset(hyp)
        det_result = critic.det_check(changeset, profile)
        verdict = review_gate.review(changeset, det_result)
        if mode == "p1": → 等待人工门③（gate_review）
        if verdict == "reject": → RepairLoop ≤5 → 仍拒转人工队列; continue

        # APPLYING
        cand_ver = applier.apply(sut_id, hyp, profile)   # 含强制快照
        hyp.status = "applied"

        # VERIFYING（三级，ADR-21）
        smoke_result = batch_runner.run_batch(smoke_batch)
        if smoke_result.failed: → REVERT short-circuit; break
        screen_result = batch_runner.run_batch(screen_batch)
        if not screen_result.passed_threshold: → REVERT; break
        holdout_result = batch_runner.run_batch(holdout_batch)
        base_result = batch_runner.run_batch(baseline_batch)  # 或读缓存

        # DECIDING
        ctx = build_decision_ctx(base_result, holdout_result, noise_bands, profile)
        decision = selector.decide(ctx)
        if mode == "p1": → 等待人工门④（gate_decision）
        execute_decision(decision)  # promote or rollback
        recorder.record(exp_id, decision, base_batch, cand_batch)
        calc_hit_rate(hyp, holdout_result)  # 回算预测命中率写 hypotheses

    # 循环或终止判断
    if consecutive_no_gain >= profile.stop.no_gain_rounds:
        → FINISHED（收敛停机）
    if budget_exceeded or stop_file:
        → FINISHED
    → 回到 ATTRIBUTING（下一轮）
```

### 5.4 人工门等待机制（P0/P1 模式）

```
状态机进入门等待时：
  iteration.state = PAUSED（DB 落库）
  iteration.current_step = "awaiting_direction" | "awaiting_hypothesis" | "awaiting_review" | "awaiting_decision"
  → WEB SSE 推送 iteration.status_changed → 用户看到"等待人工操作"
  → 用户经四门 API 提交 → 状态机恢复推进

实现：asyncio.Event + DB 轮询双保险
  primary: asyncio.Event.set() 由 gate_* 方法调用
  fallback: 每 5s 查 iterations 表 state 变化（进程重启后恢复）
```

### 5.5 三级验证编排（VERIFYING 内部）

```
async def verifying_phase(sut_id, profile, candidate_version):
    # ① smoke 批（冒烟集，活性检查）
    smoke_batch = create_batch(evalset=profile.evalsets.smoke, purpose="smoke")
    smoke_stats = await batch_runner.run_batch(smoke_batch)
    if smoke_stats.metrics["task_complete"] < 0.5:  # 过半失败
        return VerifyResult SHORT_CIRCUIT_REVERT  # 不进全量，省成本

    # ② iterate_screen 批（迭代集小子集）
    screen_batch = create_batch(evalset=profile.evalsets.iterate, purpose="iterate_screen")
    screen_stats = await batch_runner.run_batch(screen_batch)
    if not passed_threshold(screen_stats, profile):
        return VerifyResult SHORT_CIRCUIT_REVERT

    # ③ holdout 批（主评测集全量——晋升判定唯一依据）
    holdout_batch = create_batch(evalset=profile.evalsets.holdout, purpose="candidate_eval")
    holdout_stats = await batch_runner.run_batch(holdout_batch)
    return VerifyResult(holdout_stats=holdout_stats)
```

## 6. 依赖矩阵

- imports：rsi/core、rsi/sandbox、rsi/carrier、rsi/instrumentor、rsi/tracing、rsi/judge、rsi/optimizer、rsi/decision、rsi/versioning、rsi/security（AuditLog）。
- 注入：BatchRunner←全部服务；IterationEngine←BatchRunner+optimizer 组件+decision 组件+GitStore。
- DB：**写** batches、runs、iterations、experiments、hypotheses（状态流转）、checkpoints；**读** eval_tasks、evalsets、config_versions。
- 事件：EventBus 发布 batch/run/lab/iteration/instance.status_changed。
- 被依赖：api（DD-13 全部触发/控制端点）。

## 7. 测试映射与实施注记

**测试**：TU-001（BatchRunner 状态机全转换+非法转换拒绝+重试矩阵）、TU-027（IterationEngine 状态机全转换）、IN-001（Fake 全链线）、IN-002（断点续跑）、IN-003（缓存/重算）、IN-006（优化迭代 cassette）、IN-012（环境一次构建+任务后恢复）、IN-013（预算/STOP）、IN-019（自优化模式三态）、IN-020（人工门四端点）、IN-022（多实例并行）、EE-002（E2E 全链）。

**实施注记**：① run_task 内 prepare/restore_task 的重试由 BatchRunner 驱动（非 sandbox 内部——sandbox 只做单次操作，重试策略在上层）；② 人工门等待用 asyncio.Event+DB 轮询双保险（进程重启后 Event 丢失，轮询兜底恢复）；③ BudgetController 内存缓存 token 用量（写 DB 有延迟），全局熔断判断基于内存值（最终一致性可接受——差 1-2 秒不超太多）；④ LabScheduler 的"最少活跃 lab"= `SELECT backend, COUNT(*) FROM labs WHERE status IN ('building','ready') GROUP BY backend`——实时查不缓存；⑤ EventBus 有界队列满丢旧：订阅者处理慢时可能漏中间态（但总能收到最新态——语义正确）；⑥ checkpoint_interval_s 合并写：连续任务完成间隔 < 5s 时不写 DB（内存暂存，下个间隔点批量刷）。
