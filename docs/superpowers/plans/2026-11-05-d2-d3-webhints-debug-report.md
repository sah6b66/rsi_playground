# D2 + D3 Report — Web Input Hints & Debug Logging

Date: 2026-11-05 (executed 2026-11-02)
Branch: `feat/d2-d3-webhints-debug`

## Status: COMPLETE

## D2: Web input hints

Added `json_schema_extra={"example": ...}` to Pydantic fields so Swagger UI shows realistic example values:

- `platform/rsi/api/routers/suts.py` — SutCreate / SutUpdate (name="ipops-agent", carrier="opencode", image_ref="ipops-agent:v1.2")
- `platform/rsi/api/routers/evalsets.py` — EvalsetCreate / EvalsetUpdate (task list examples)
- `platform/rsi/api/routers/profiles.py` — ProfilePatch (all 8 fields)
- `platform/rsi/api/routers/batches.py` — BatchCreate (all 5 fields)
- `platform/rsi/api/routers/iterations.py` — IterationCreate + GateBody (generic gate payload used by all four gate endpoints)
- `platform/rsi/core/models.py` — OptimizationProfile family (Goal, EvalsetBinding, StopPolicy, Budget, LlmCompare, OptimizationProfile) since `POST /profiles` takes OptimizationProfile directly
- `platform/rsi/api/deps.py` — inspected; contains no request models (DI container only), no changes needed

All defaults/validation semantics preserved (no new defaults on previously-required fields).

## D3: Debug logging

Instrumented all 30 files listed in the task with `rsi.log.get_logger` debug calls:
entry params (long strings truncated to 200 chars), exit summaries
(result type / counts / booleans, never full LLM bodies, credentials, or trace
payloads), branch decisions (cache_hit n/a; backend_selected, retry_round,
rule_hit, strategy, verify/degrade, circuit_breaker, abort reasons), and
exception types on error paths before re-raise.

Groups committed separately (sandbox, carrier, tracing+judge,
decision+optimizer+orchestrator+instrumentor).

## Commits

1. `9f0a078` D2: add Swagger example values to API request models
2. `b41912d` D3 group 1: sandbox modules (service, fake backend, readiness, config_api, scheduler)
3. `eaba6f8` D3 group 2: carrier modules (service, hosts, network, scheduler, extract, overlay)
4. `a9548af` D3 group 3: tracing + judge (collector, store, parsers, api, deterministic, safety, llm_judge, antihack, aggregate)
5. `218b730` D3 group 4: decision, optimizer, orchestrator, instrumentor (aggregator, selector, targets, attribution, batch, iteration, budget, event_bus, checkpoint, facade)

## Test summary

`uv run --no-sync pytest tests/` from `platform/` run after D2 and after each D3 group:
**655 passed, 130 warnings** every run (warnings are pre-existing SQLModel deprecation warnings).

## Concerns

- `iterations.py` gate endpoints share one generic `GateBody` model, so all four gates (confirm-direction, hypothesis, review, decision) show the same combined example object; extra fields are allowed so this is accurate but slightly generic.
- `models.py` (core domain models) now carries `json_schema_extra` examples; this is OpenAPI metadata only and does not affect validation, but it does touch a shared core file rather than only the API layer.
- Debug logging adds one `logger.debug` call per hot loop iteration in a few spots (e.g., `TraceCollector.ingest` logs once per call not per event; parsers log only on validation errors) — kept at call granularity to avoid noisy per-event output. Debug is off by default.
