# Plan 2: RSI Playground Execution Layer (S3+S4+S5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the execution layer — sandbox simulation abstraction (FakeSandbox + readiness), SUT carrier (HostService + OpenCodeAdapter + Fake), and trace collection (collector + wiretap parser + seq) — enabling offline full-chain testing without real network devices or LLM.

**Architecture:** Three independent modules consuming Plan 1 foundation. FakeSandbox provides in-memory virtual devices (rule-table-driven). FakeCarrier replays cassette results. TraceCollector tails NDJSON files and assigns monotonic seq. All testable offline.

**Tech Stack:** Existing Plan 1 deps + httpx (for ConfigApiCaller). No new heavy deps — netmiko/paramiko not needed for Fake implementations (P0 offline).

**Spec:** DD-04 (sandbox), DD-05 (carrier), DD-07 (tracing), plus design doc §3.2/3.4/3.5.

## Global Constraints

- All code under `platform/rsi/` (existing structure from Plan 1)
- Tests under `platform/tests/unit/` — no Docker, no network, no LLM, no real SSH
- All async methods use `asyncio` (sync libs via `asyncio.to_thread` if needed)
- Existing Plan 1 tests (283) must continue passing
- FakeSandboxBackend is THE test infrastructure for all downstream modules (Plan 3+)
- Commit after each green test cycle

---

### Task 1: Sandbox — VendorProfile + SshPool (offline mock)

**Files:**
- Create: `platform/rsi/sandbox/__init__.py`
- Create: `platform/rsi/sandbox/vendor.py`
- Create: `platform/rsi/sandbox/sshpool.py`
- Test: `platform/tests/unit/test_sshpool.py`

**Interfaces:**
- Produces: `VendorProfile` (netmiko_device_type, error_patterns, reset_cmd, enter/exit_config), `VENDOR_MAP`, `SshPool` (device/host session pool with per-session lock), `DeviceSession` (exec_cli/exec_config/capture_config), `SessionKey(ip, port, lab_id)`, `ExecResult(ok, output, matched_errors, duration_ms)`

- [ ] Step 1: Write vendor.py — VENDOR_MAP with vrp/ce/ios profiles, error patterns per DD-04 §3
- [ ] Step 2: Write sshpool.py — SshPool with SessionKey=(ip,port,lab_id), per-session asyncio.Lock, close_lab(lab_id) for batch cleanup
- [ ] Step 3: DeviceSession wraps netmiko ConnectHandler via `asyncio.to_thread` — but P0 offline: provide a `MockDeviceSession` for testing that returns canned ExecResult
- [ ] Step 4: Tests: vendor map completeness, session key uniqueness, mock session behavior
- [ ] Step 5: Commit

### Task 2: Sandbox — ReadinessChecker + ConfigApiCaller

**Files:**
- Create: `platform/rsi/sandbox/readiness.py`
- Create: `platform/rsi/sandbox/config_api.py`
- Test: `platform/tests/unit/test_readiness.py`
- Test: `platform/tests/unit/test_config_api.py`

**Interfaces:**
- Produces: `ReadinessChecker.check(env, endpoints, timeout_s) -> ReadyReport` (ssh_reachable default, cli_match per device); `ConfigApiCaller.apply(env, api_key, params, undo=False) -> dict` (httpx POST with auth_ref resolution, undo flag)

- [ ] Step 1: ReadinessChecker — interpret ReadyCheck list, ssh_reachable uses TCP connect, cli_match uses DeviceSession.exec_cli + expect string
- [ ] Step 2: ConfigApiCaller — httpx POST to base_url/apply with JSON body, auth from CredentialStore if auth_ref, undo flag in body
- [ ] Step 3: Tests with respx mock for HTTP, mock sessions for SSH
- [ ] Step 4: Commit

### Task 3: Sandbox — FakeSandboxBackend + SandboxService

**Files:**
- Create: `platform/rsi/sandbox/backends/__init__.py`
- Create: `platform/rsi/sandbox/backends/fake.py`
- Create: `platform/rsi/sandbox/service.py`
- Test: `platform/tests/unit/test_sandbox_service.py`

**Interfaces:**
- Consumes: SshPool, ReadinessChecker, ConfigApiCaller from Tasks 1-2
- Produces: `SandboxService` (build_lab, prepare, restore_task, snapshot, restore, reset, destroy, wait_ready, exec_readonly, health_check, backend_status); `FakeSandboxBackend` (in-memory devices with rule-table outputs); `LabScheduler.select(env, backend_key) -> LabSelection`

- [ ] Step 1: FakeSandboxBackend — in-memory dict of devices, each with config_lines set + running state. exec(cli) returns rule-table output. build/wait_ready/snapshot(degraded)/restore/reset/destroy all in-memory
- [ ] Step 2: SandboxService — orchestrates backend calls, maintains ops.jsonl journal, prepare (backup→execute), restore_task (vendor template recovery), L2 fallback
- [ ] Step 3: LabScheduler — select least-active backend instance (ADR-24)
- [ ] Step 4: Tests: build→prepare→exec_readonly→restore_task cycle; fault injection via exec_cli; L2 degraded restore; concurrent labs isolation
- [ ] Step 5: Commit

### Task 4: Carrier — HostPool + NetPolicy (offline)

**Files:**
- Create: `platform/rsi/carrier/__init__.py`
- Create: `platform/rsi/carrier/hosts.py`
- Create: `platform/rsi/carrier/network.py`
- Test: `platform/tests/unit/test_carrier_network.py`

**Interfaces:**
- Produces: `HostPool` (main=npipe local, check_reach), `NetPolicy` (ensure_lab_net, ensure_trace_net, attach — P0: in-memory tracking only, no real Docker)

- [ ] Step 1: HostPool — P0 stub with main host, check_reach returns True (offline)
- [ ] Step 2: NetPolicy — in-memory set of created networks, attach records membership
- [ ] Step 3: Tests for network lifecycle
- [ ] Step 4: Commit

### Task 5: Carrier — FinalJsonExtractor + OverlayBuilder

**Files:**
- Create: `platform/rsi/carrier/extract.py`
- Create: `platform/rsi/carrier/overlay.py`
- Test: `platform/tests/unit/test_extract.py`
- Test: `platform/tests/unit/test_overlay.py`

**Interfaces:**
- Produces: `extract_task_result(stdout) -> TaskResult` (FINAL_JSON tag priority → last JSON object → parse_error); `OverlayBuilder.build(config_dir, plan) -> Path` (copy-on-write + overlay.json metadata)

- [ ] Step 1: extract.py — regex for <FINAL_JSON>...</FINAL_JSON>, fallback balanced-brace scan, map to TaskResult fields
- [ ] Step 2: overlay.py — shutil.copytree to overlay dir, write overlay.json with source version + destroyable marker
- [ ] Step 3: Tests: valid FINAL_JSON, no tag → last JSON fallback, empty stdout → parse_error, overlay doesn't touch source
- [ ] Step 4: Commit

### Task 6: Carrier — HostService + FakeCarrier

**Files:**
- Create: `platform/rsi/carrier/service.py`
- Create: `platform/rsi/carrier/adapters/__init__.py`
- Create: `platform/rsi/carrier/adapters/fake.py`
- Test: `platform/tests/unit/test_host_service.py`

**Interfaces:**
- Consumes: HostPool, NetPolicy, OverlayBuilder, extract_task_result from Tasks 4-5; GitStore.export from Plan 1
- Produces: `HostService` (create_instance, submit_task, collect, destroy); `FakeCarrier` (cassette-replay adapter); `CarrierAdapter` ABC

- [ ] Step 1: CarrierAdapter ABC — build_image, start, submit_task, collect_result, trace_channels
- [ ] Step 2: FakeCarrier — accepts list of (TaskSpec → stdout) mappings, replays on submit_task, collect_result extracts via FinalJsonExtractor
- [ ] Step 3: HostService — creates InstanceHandle (with mock container ID), calls adapter for task execution, tracks lifecycle
- [ ] Step 4: Tests: full lifecycle create→submit→collect→destroy; timeout handling; parse_error propagation; instance_crashed flag
- [ ] Step 5: Commit

### Task 7: Tracing — SeqAllocator + TraceEvent NDJSON parser

**Files:**
- Create: `platform/rsi/tracing/__init__.py`
- Create: `platform/rsi/tracing/seq.py`
- Create: `platform/rsi/tracing/parsers.py`
- Test: `platform/tests/unit/test_seq.py`
- Test: `platform/tests/unit/test_parsers.py`

**Interfaces:**
- Produces: `SeqAllocator.next(run_id) -> int` (per-run monotonic); `OpenCodeWiretapParser.parse_line(line) -> TraceEvent | None` (assembled/http frames); `NdjsonEventParser.parse_line(line) -> TraceEvent` (pg_trace/plugin format)

- [ ] Step 1: SeqAllocator — dict[run_id, int], increment and return
- [ ] Step 2: OpenCodeWiretapParser — handle assembled(kind=context)→llm_request with system/messages/tools, http(kind=primary)→body, filter title/compaction/generate
- [ ] Step 3: NdjsonEventParser — direct TraceEvent from plugin/hook JSON line
- [ ] Step 4: Tests: seq monotonic per run, wiretap frame parsing (both types, kind filtering, protocol detection), event parser round-trip
- [ ] Step 5: Commit

### Task 8: Tracing — TraceCollector + TraceStore

**Files:**
- Create: `platform/rsi/tracing/collector.py`
- Create: `platform/rsi/tracing/store.py`
- Test: `platform/tests/unit/test_trace_collector.py`
- Test: `platform/tests/unit/test_trace_store.py`

**Interfaces:**
- Consumes: SeqAllocator, parsers from Task 7
- Produces: `TraceCollector` (tail_ndjson, ingest, finalize, WS hub); `TraceStore` (query, stats — in-memory for P0)

- [ ] Step 1: TraceCollector.ingest — parse → seq assign → append to events list (in-memory per run) → notify WS subscribers
- [ ] Step 2: TraceCollector.finalize — mark run as complete, write meta (event_count, time_range)
- [ ] Step 3: TraceStore.query — filter by run_id/event_type/is_error; TraceStore.stats — aggregate tokens/steps/duration
- [ ] Step 4: Tests: ingest produces monotonic seq, finalize sets meta, query filters correctly, stats aggregates
- [ ] Step 5: Commit

### Task 9: Integration — Sandbox+Carrier+Tracing offline chain

**Files:**
- Test: `platform/tests/integration/test_execution_layer.py`

- [ ] Step 1: Write integration test: build FakeSandbox → prepare a task → create FakeCarrier instance → submit task → collect result → finalize trace → verify TraceStore has events
- [ ] Step 2: Run all tests (Plan 1 283 + Plan 2 new) — must all pass
- [ ] Step 3: Commit
