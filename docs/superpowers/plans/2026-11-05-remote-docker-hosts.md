# Remote Docker Hosts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Implement multi-host Docker client pool (local npipe + remote SSH tunnels), InstanceScheduler (least-loaded + max_instances_per_host), and per-host NetPolicy — enabling SUT Agent containers to run on local Windows Docker Desktop AND multiple remote Linux servers simultaneously.

**Spec:** DD-05 v1.2 §4.1 (R1-R4), config doc v1.5 §2③.

## Tasks

### Task 1: HostPool multi-host Docker clients + InstanceScheduler
**Files:** `rsi/carrier/hosts.py` (rewrite), `rsi/carrier/scheduler.py` (new)
**Test:** `tests/unit/test_hosts_scheduler.py`

### Task 2: Per-host NetPolicy + HostService cross-host routing
**Files:** `rsi/carrier/network.py` (rewrite), `rsi/carrier/service.py` (modify)
**Test:** `tests/unit/test_network_crosshost.py`, update `test_host_service.py`

---
