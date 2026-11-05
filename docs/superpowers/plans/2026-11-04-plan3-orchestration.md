# Plan 3: RSI Playground Orchestration + Judgment (S6+S6b+S7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Build the closed-loop core — LLM client (with cassette), Judge (3-layer verdict), Decision (selector), Optimizer (rule attribution + target registry), BatchRunner (9-step pipeline), and IterationEngine (state machine + human gates).

**Architecture:** Consumes Plan 1 (core/security/versioning) and Plan 2 (sandbox/carrier/tracing). LLM calls mocked via cassette for offline testing. Judge uses FakeSandbox for device state assertions. BatchRunner orchestrates the full task lifecycle. IterationEngine drives the optimization loop.

**Tech Stack:** Existing deps + no new heavy deps. LLM cassette = in-memory mock (no real API calls).

**Spec:** DD-02 (llm), DD-08 (judge), DD-10 (decision), DD-11 (optimizer), DD-12 (orchestrator).

## Global Constraints
- All 420 existing tests must continue passing
- Offline only: no Docker, no network, no real LLM API calls
- LLM points use cassette mock (LLMClient with injectable responses)
- FakeSandboxBackend for all device interaction
- Commit after each green test cycle

## Dispatch Rule (v2 — 用户决策)
Code-generation dispatches reference **only DD files** (detailed design). No upstream FR/ADR/configuration references in implementer prompts. Upstream docs are for controller review only. Missing DD context is either added to DD §7 (Implementation Notes) or inlined in the dispatch as "Additional Context".

---

### Task 1: EventBus + CheckpointStore
**Files:** `rsi/orchestrator/__init__.py`, `rsi/orchestrator/event_bus.py`, `rsi/orchestrator/checkpoint.py`
**Test:** `tests/unit/test_event_bus.py`, `tests/unit/test_checkpoint.py`
**Produces:** `EventBus(publish, publish_alert, subscribe, unsubscribe)`, `CheckpointStore(save, resume)` — in-memory P0

### Task 2: BudgetController
**Files:** `rsi/orchestrator/budget.py`
**Test:** `tests/unit/test_budget.py`
**Produces:** `BudgetController(check_and_reserve, record, check_global, halt, check_stop_file)` — scope + global + STOP file

### Task 3: LLM Client (cassette mock)
**Files:** `rsi/llm/__init__.py`, `rsi/llm/client.py`
**Test:** `tests/unit/test_llm_client.py`
**Produces:** `LLMClient.complete(component, prompt_name, vars, schema) -> LLMResponse` with injectable `_handler` callable for offline mock. CassetteStore as dict-based cache. PromptLibrary with simple string templates.

### Task 4: Judge — DeterministicJudge + SafetyScanner
**Files:** `rsi/judge/__init__.py`, `rsi/judge/deterministic.py`, `rsi/judge/safety.py`
**Test:** `tests/unit/test_judge_det.py`, `tests/unit/test_judge_safety.py`
**Produces:** `DeterministicJudge.evaluate(task, result, lab, sandbox) -> DetScores` (final_state assertions + critical_steps hit count); `SafetyScanner.scan(events, permission) -> SafetyVerdict` (fatal/major/minor rules)

### Task 5: Judge — LLMJudge + AntiHack + Aggregator + JudgeService
**Files:** `rsi/judge/llm_judge.py`, `rsi/judge/antihack.py`, `rsi/judge/aggregate.py`, `rsi/judge/api.py`
**Test:** `tests/unit/test_judge_full.py`
**Produces:** `LLMJudge.judge(task, result, events, coverage) -> SemanticScores` (root_cause match + path quality + checklist + consistency); `AntiHackDetector.detect(...) -> HackSignals`; `VerdictAggregator.aggregate(...) -> Verdict`; `JudgeService.judge_run(run_id) -> Verdict`

### Task 6: Decision — StatsAggregator + RegularizedSelector
**Files:** `rsi/decision/__init__.py`, `rsi/decision/aggregator.py`, `rsi/decision/selector.py`
**Test:** `tests/unit/test_decision.py`
**Produces:** `StatsAggregator.aggregate(verdicts) -> BatchStats`; `RegularizedSelector.decide(ctx) -> Decision` (rules 0-6)

### Task 7: Optimizer — TargetRegistry + RuleAttributionAnalyzer
**Files:** `rsi/optimizer/__init__.py`, `rsi/optimizer/targets.py`, `rsi/optimizer/attribution.py`
**Test:** `tests/unit/test_optimizer.py`
**Produces:** `TargetRegistry.validate(changeset, enabled_targets)` + `TARGET_PATHS`; `AttributionAnalyzer.analyze(failed_runs, sut_config, opt_history) -> AttributionReport` (rule layer P0)

### Task 8: BatchRunner — run_batch + run_task
**Files:** `rsi/orchestrator/batch.py`
**Test:** `tests/unit/test_batch_runner.py`
**Produces:** `BatchRunner.run_batch(batch_id) -> BatchStats` (build lab → prepare → create instance → submit → collect → trace → judge → restore → checkpoint); `BatchRunner.run_task(batch_id, task, seq) -> RunResult`

### Task 9: IterationEngine + Integration Test
**Files:** `rsi/orchestrator/iteration.py`, `tests/integration/test_closed_loop.py`
**Test:** integration
**Produces:** `IterationEngine.iterate_once(sut_id, profile, mode) -> ExperimentRecord`; integration test exercising full offline closed loop

---
