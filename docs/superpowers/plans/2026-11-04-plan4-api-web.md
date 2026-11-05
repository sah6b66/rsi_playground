# Plan 4: RSI Playground Instrumentor + API/WEB (S8+S9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Build the instrumentation pipeline (scanner/planner/injector/verifier) and the FastAPI Platform API layer (19 routers + SSE/WS + OpenAPI), enabling WEB-based operation of the platform.

**Architecture:** Instrumentor operates on overlay copies (ADR-4). API layer is a thin wrapper over Plan 1-3 services using FastAPI dependency injection. All tests use Fake implementations — no Docker/network/LLM.

**Tech Stack:** Existing deps + FastAPI (already added in Plan 1). httpx test client for API tests.

**Spec:** DD-06 (instrumentor), DD-13 (api).

## Global Constraints
- All 545 existing tests must continue passing
- Offline only; DD files are the sole spec source (dispatch rule v2)
- Commit after each green test cycle

---

### Task 1: Instrumentor — Scanner + Planner + Injector + Verifier
**Files:** `rsi/instrumentor/__init__.py`, `scanner.py`, `planner.py`, `injector.py`, `verifier.py`, `report.py`
**Test:** `tests/unit/test_instrumentor.py`
**Produces:** `Instrumentor.instrument(config_dir, carrier, smoke_task) -> (overlay_dir, CoverageReport)`

### Task 2: API — App factory + auth router + suts router
**Files:** `rsi/api/__init__.py`, `app.py`, `deps.py`, `errors.py`, `routers/auth.py`, `routers/suts.py`
**Test:** `tests/unit/test_api_auth.py`, `tests/unit/test_api_suts.py`
**Produces:** `create_app(services) -> FastAPI`; `POST /auth/login`, `GET /auth/me`; SUT CRUD

### Task 3: API — evalsets + profiles + batches + runs routers
**Files:** `routers/evalsets.py`, `routers/profiles.py`, `routers/batches.py`, `routers/runs.py`
**Test:** `tests/unit/test_api_core.py`
**Produces:** evalset CRUD/freeze/import; profile CRUD; batch trigger/pause/abort; run detail/traces/re-judge

### Task 4: API — iterations + labs + instances + judgments + experiments + traces routers
**Files:** `routers/iterations.py`, `routers/labs.py`, `routers/instances.py`, `routers/judgments.py`, `routers/experiments.py`, `routers/traces.py`
**Test:** `tests/unit/test_api_ext.py`
**Produces:** iteration lifecycle + 4 human gates; lab management; instance list; judgment query; experiment compare; trace diff/query/stats; SSE/WS

### Task 5: API — system + dashboard + audit + instrumentation routers + SSE/WS
**Files:** `routers/system.py`, `routers/dashboard.py`, `routers/audit_logs.py`, `routers/instrumentation.py`, `streaming.py`
**Test:** `tests/unit/test_api_system.py`
**Produces:** settings/stop/freeze/logs/health; dashboard metrics; audit query; coverage report; SSE events + WS trace stream

### Task 6: API Integration Test
**Files:** `tests/integration/test_api.py`
**Test:** Full API integration — login → create SUT → create evalset → trigger batch → check status → list runs → get judgment → get trace → dashboard

---
