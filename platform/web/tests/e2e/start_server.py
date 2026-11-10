"""Playwright e2e 离线后端（T8 / EE-007）。

以 build_services() 离线装配 FastAPI app 并：
1. 种入已知账号 admin / Playwright123!（services["users"]，argon2 哈希）；
2. 内存 stores 预置 e2e 数据：1 SUT + 1 评测集 + 1 个 PAUSED@awaiting_hypothesis
   会话及其 timeline 数据（batches/runs/experiments，形态照
   platform/tests/unit/test_web_timeline.py 的 store 注入方式）。

监听 127.0.0.1:8765，由 web/playwright.config.ts 的 webServer 拉起。
运行：uv run python web/tests/e2e/start_server.py（cwd=platform）
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from rsi.api.app import create_app  # noqa: E402
from rsi.bootstrap import build_services  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "Playwright123!"
HOST = "127.0.0.1"
PORT = 8765


def _now(n: int = 1) -> str:
    return f"2026-11-05T0{n}:00:00+00:00"


def seed(app) -> dict[str, str]:
    """注入 admin 账号与 e2e 数据；返回 {sut_id, evalset_id, iteration_id}。"""
    services = app.state.services
    auth = services["auth_service"]
    services["users"][ADMIN_USER] = {
        "user_id": "u-e2e-admin",
        "role": "admin",
        "password_hash": auth.hash_password(ADMIN_PASSWORD),
        "disabled": False,
    }

    stores = app.state.stores
    sut_id = f"sut-{uuid.uuid4().hex[:8]}"
    stores["suts"][sut_id] = {
        "id": sut_id,
        "name": "e2e-agent",
        "carrier": "linux",
        "image_ref": "e2e-agent:v1",
        "description": "playwright e2e seeded SUT",
        "created_at": _now(),
    }
    # 向导创建用第二个 SUT（同一 SUT 已有活跃迭代会被 409 互斥拦截）
    sut2_id = f"sut-{uuid.uuid4().hex[:8]}"
    stores["suts"][sut2_id] = {
        "id": sut2_id,
        "name": "e2e-agent-2",
        "carrier": "linux",
        "image_ref": "e2e-agent:v2",
        "description": "playwright e2e wizard SUT",
        "created_at": _now(),
    }
    evalset_id = f"es-{uuid.uuid4().hex[:8]}"
    stores["evalsets"][evalset_id] = {
        "id": evalset_id,
        "name": "e2e-evalset",
        "tasks": [
            {"id": "t-001", "prompt": "Diagnose why BGP peer 10.0.0.2 is down"},
            {"id": "t-002", "prompt": "Find the interface with the highest error rate"},
        ],
        "description": "playwright e2e seeded evalset",
        "frozen": False,
        "version": 1,
        "created_at": _now(),
        "versions": [],
    }

    it_id = f"iter-{uuid.uuid4().hex[:8]}"
    stores["iterations"][it_id] = {
        "id": it_id,
        "sut_id": sut_id,
        "profile_id": "prof-e2e",
        "evalset_id": evalset_id,
        "profile": None,
        "mode": "p1",
        # 门①已过（gates 记 1 条）→ 暂停在门② awaiting_hypothesis
        "state": "paused",
        "current_step": "awaiting_hypothesis",
        "round": 2,
        "experiments": ["exp-e2e-1"],
        "budget_used": {"llm_calls": 0, "tokens": 600},
        "archived": False,
        "paused_from": "attributing",
        "gates": [
            {
                "gate": "confirm-direction",
                "step": "awaiting_direction",
                "by": "u-e2e-admin",
                "at": _now(),
            }
        ],
        "created_at": _now(),
    }

    # timeline 数据：2 轮 batches/runs + 1 experiment（含版本与噪声带）
    for n in (1, 2):
        stores["batches"][f"bat-e2e-{n}"] = {
            "id": f"bat-e2e-{n}",
            "evalset_id": evalset_id,
            "sut_id": sut_id,
            "purpose": "iterate",
            "profile_id": None,
            "status": "finalized",
            "paused": False,
            "progress": {},
            "created_at": _now(n),
            "iteration_id": it_id,
            "round": n,
        }
    stores["runs"]["run-e2e-1"] = {
        "id": "run-e2e-1",
        "batch_id": "bat-e2e-1",
        "task_id": "t-001",
        "status": "succeeded",
        "result": {"tokens": 100, "latency_ms": 1200},
        "verdict": {
            "verdict": "pass",
            "score": 1.0,
            "reasons": [],
            "dimensions": {"root_cause_match": True},
        },
    }
    stores["runs"]["run-e2e-2"] = {
        "id": "run-e2e-2",
        "batch_id": "bat-e2e-1",
        "task_id": "t-002",
        "status": "succeeded",
        "result": {"tokens": 200, "latency_ms": 1800},
        "verdict": {
            "verdict": "reject",
            "score": 0.0,
            "reasons": [],
            "dimensions": {"root_cause_match": False},
        },
    }
    stores["runs"]["run-e2e-3"] = {
        "id": "run-e2e-3",
        "batch_id": "bat-e2e-2",
        "task_id": "t-001",
        "status": "succeeded",
        "result": {"tokens": 300, "latency_ms": 900},
        "verdict": {
            "verdict": "pass",
            "score": 1.0,
            "reasons": [],
            "dimensions": {"root_cause_match": True},
        },
    }
    stores["experiments"]["exp-e2e-1"] = {
        "id": "exp-e2e-1",
        "sut_id": sut_id,
        "iteration_id": it_id,
        "versions": {
            "a": {"version": 1},
            "b": {"version": 2, "noise_band": [0.6, 0.7]},
        },
        "decision": "apply",
        "evidence": [],
        "files_changed": [{"path": "skill/bgp.md", "target": "skill"}],
    }
    return {
        "sut_id": sut_id,
        "sut2_id": sut2_id,
        "evalset_id": evalset_id,
        "iteration_id": it_id,
    }


def main() -> None:
    services = build_services()
    app = create_app(services=services)
    ids = seed(app)
    print(
        f"[e2e] seeded admin/{ADMIN_PASSWORD!r} + "
        f"suts={ids['sut_id']},{ids['sut2_id']} "
        f"evalset={ids['evalset_id']} "
        f"iteration={ids['iteration_id']} (paused@awaiting_hypothesis)",
        flush=True,
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
