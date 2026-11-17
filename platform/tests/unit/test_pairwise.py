"""B2-T6：pairwise 对比判决——模板渲染/刷分标记/同任务校验/cassette 回归.

覆盖（FR7.2 P1，DD-08 pairwise 旁路增强）：
* LLMJudge.pairwise 产 PairwiseReport——注入 handler 断言 component/prompt/
  vars 五键渲染、winner/hack_suspect 断言；
* 模板占位符契约——真实 PromptLibrary 五缺一即 ConfigError；
* 端点四形态——同任务 200 / 跨任务 422 / 事件缺失 422 / LLM 失败 503；
* cassette record→replay roundtrip（ADR-7 回归资产模式，同 T2）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.core.errors import ConfigError
from rsi.core.models import PairwiseReport
from rsi.judge.antihack import AntiHackDetector
from rsi.judge.llm_judge import LLMJudge
from rsi.llm.client import LLMClient
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "rsi" / "llm" / "prompts"

_PW = {
    "winner": "candidate",
    "root_cause_dim": "candidate 根因表述更完整",
    "path_quality_dim": "baseline 漏查接口状态",
    "consistency_dim": "双方结论-过程一致",
    "rationale": "候选版本补齐关键步骤",
    "hack_suspect": True,
    "hack_reasons": ["跳过验证直接给结论"],
}

_VARS_KEYS = {
    "task_title",
    "expected",
    "baseline_summary",
    "candidate_summary",
    "antihack_signals",
}

_TASK = {
    "task_id": "t1",
    "title": "接口故障定位",
    "expected": {"root_cause": "GE1/0/3 down"},
}
_BASE = ({"answer": "b"}, [{"kind": "TOOL_CALL", "payload": {"command": "x"}}])
_CAND = ({"answer": "c"}, [{"kind": "TOOL_CALL", "payload": {"command": "y"}}])


# ---------------------------------------------------------------------------
# LLMJudge.pairwise（方法层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairwise_produces_report():
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen.update(component=component, prompt=prompt_name, vars=vars)
        return json.dumps(_PW, ensure_ascii=False)

    judge = LLMJudge(LLMClient(handler=handler))
    report = await judge.pairwise(
        dict(_TASK), _BASE, _CAND, antihack_signals="skipped_steps=0; missing_trial=false"
    )
    assert isinstance(report, PairwiseReport)
    assert report.winner == "candidate" and report.hack_suspect is True
    assert report.root_cause_dim == "candidate 根因表述更完整"
    assert report.hack_reasons == ["跳过验证直接给结论"]
    assert seen["component"] == "judge" and seen["prompt"] == "pairwise"
    # 五键 vars 契约——与模板占位符一一对应
    assert _VARS_KEYS <= set(seen["vars"])
    assert seen["vars"]["task_title"] == "接口故障定位"
    assert "GE1/0/3 down" in seen["vars"]["expected"]
    assert "command=x" in seen["vars"]["baseline_summary"]
    assert "command=y" in seen["vars"]["candidate_summary"]
    assert "answer=c" in seen["vars"]["candidate_summary"]
    assert seen["vars"]["antihack_signals"] == "skipped_steps=0; missing_trial=false"


@pytest.mark.asyncio
async def test_pairwise_accepts_evaltask_and_trace_events():
    """EvalTask 包装对象 + TraceEvent 列表（生产形态）同样可用。"""
    from rsi.core.enums import EventType
    from rsi.core.models import (
        DeviceSpec,
        EnvSpec,
        EvalTask,
        ExpectedAnswer,
        RootCauseExpectation,
        SshAccess,
        TaskRecord,
        TaskSpec,
        Topology,
        TraceEvent,
    )

    env = EnvSpec(
        name="env",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(),
        ssh_access={"R1": SshAccess(ip="127.0.0.1", username="u")},
    )
    rec = TaskRecord(
        task_id="fault/demo-01",
        title="demo",
        task=TaskSpec(prompt="p"),
        expected=ExpectedAnswer(root_cause=RootCauseExpectation(expected="R1 GE0/0/1 down")),
    )
    task = EvalTask(env=env, task=rec)
    events = [
        TraceEvent(
            run_id="r1", task_id="fault/demo-01", lab_id="lab", sut_id="sut",
            seq=0, ts="2026-01-01T00:00:00.000+00:00",
            event_type=EventType.TOOL_CALL,
            data={"name": "ssh", "args": {"cli": "display interface"}},
        )
    ]
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen.update(vars=vars)
        return json.dumps(_PW, ensure_ascii=False)

    judge = LLMJudge(LLMClient(handler=handler))
    report = await judge.pairwise(
        task, ({"root_cause": "b"}, events), ({"root_cause": "c"}, events)
    )
    assert isinstance(report, PairwiseReport)
    assert seen["vars"]["task_title"] == "demo"
    assert "display interface" in seen["vars"]["baseline_summary"]
    assert seen["vars"]["antihack_signals"] == ""  # 缺省空串


def test_template_placeholder_contract():
    """真实模板五占位符与 vars 一致；缺一即 ConfigError（F3 契约）。"""
    from rsi.llm.prompts import PromptLibrary

    lib = PromptLibrary()
    lib.load_root(PROMPTS_ROOT)
    assert lib.latest_version("judge", "pairwise") >= 1
    vars = {k: "v" for k in _VARS_KEYS}
    out = lib.render("judge", "pairwise", dict(vars))
    assert "candidate" in out and "hack_suspect" in out  # 中文正文+英文键名
    bad = {k: v for k, v in vars.items() if k != "expected"}
    with pytest.raises(ConfigError):
        lib.render("judge", "pairwise", bad)


@pytest.mark.asyncio
async def test_cassette_record_replay_roundtrip(tmp_path):
    from rsi.llm.cassette import CassetteStore

    cs = CassetteStore(root=tmp_path, mode="record")
    llm = LLMClient(
        handler=lambda *a, **k: json.dumps(_PW, ensure_ascii=False), cassette=cs
    )
    report = await LLMJudge(llm).pairwise(
        dict(_TASK), _BASE, _CAND, antihack_signals="s=0"
    )
    assert report.winner == "candidate"
    # replay：换一个会抛错的 handler，命中卡带仍得相同结果
    llm2 = LLMClient(
        handler=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")),
        cassette=CassetteStore(root=tmp_path, mode="replay"),
    )
    report2 = await LLMJudge(llm2).pairwise(
        dict(_TASK), _BASE, _CAND, antihack_signals="s=0"
    )
    assert report2 == report


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/pairwise（端点层）
# ---------------------------------------------------------------------------


def _ok_handler(component, prompt_name, vars, *, temperature=0.0):
    return json.dumps(_PW, ensure_ascii=False)


def _bad_handler(component, prompt_name, vars, *, temperature=0.0):
    return "not json at all"


def make_services(handler=_ok_handler) -> dict:
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "llm_judge": LLMJudge(LLMClient(handler=handler)),
        # B2-T6 修复：与 bootstrap 装配一致——pairwise 信号通道取此键
        "antihack": AntiHackDetector(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
            # B2 终审 Fix3：无 iteration:manage 角色（仅 view:read）
            "researcher": {
                "user_id": "u-researcher",
                "role": "researcher",
                "password_hash": auth.hash_password("researcher-pw"),
            },
        },
    }


@pytest_asyncio.fixture
async def client():
    services = make_services()
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _seed_pair(client, task_a="t-1", task_b=None, with_traces=True):
    stores = client._transport.app.state.stores
    stores["runs"]["ra"] = {
        "id": "ra", "batch_id": "b", "task_id": task_a, "status": "done",
        "result": {"root_cause": "GE1/0/3 down"},
    }
    stores["runs"]["rb"] = {
        "id": "rb", "batch_id": "b", "task_id": task_b or task_a, "status": "done",
        "result": {"root_cause": "GE1/0/3 端口故障"},
    }
    if with_traces:
        stores["traces"]["ra"] = [
            {"seq": 1, "type": "tool_call", "data": {"command": "display version"}},
        ]
        stores["traces"]["rb"] = [
            {"seq": 1, "type": "tool_call", "data": {"command": "display interface"}},
        ]


async def test_endpoint_same_task_returns_report(client):
    h = await admin_headers(client)
    _seed_pair(client)
    resp = await client.post(
        "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}, headers=h
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["winner"] == "candidate"
    assert body["hack_suspect"] is True
    assert body["hack_reasons"] == ["跳过验证直接给结论"]


async def test_endpoint_cross_task_422(client):
    h = await admin_headers(client)
    _seed_pair(client, task_a="t-1", task_b="t-2")
    resp = await client.post(
        "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}, headers=h
    )
    assert resp.status_code == 422, resp.text
    assert "different eval tasks" in resp.json()["message"]


async def test_endpoint_missing_events_422(client):
    h = await admin_headers(client)
    _seed_pair(client, with_traces=False)
    resp = await client.post(
        "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}, headers=h
    )
    assert resp.status_code == 422, resp.text
    assert "trace events" in resp.json()["message"]


async def test_endpoint_llm_invalid_503(client):
    services = make_services(handler=_bad_handler)
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        h = await admin_headers(c)
        _seed_pair(c)
        resp = await c.post(
            "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}, headers=h
        )
        assert resp.status_code == 503, resp.text


async def test_endpoint_unknown_run_404(client):
    h = await admin_headers(client)
    resp = await client.post(
        "/api/v1/runs/zz/pairwise", json={"other_run_id": "rb"}, headers=h
    )
    assert resp.status_code == 404, resp.text


async def test_endpoint_antihack_signals_skipped_steps():
    """B2-T6 修复回归：candidate 跳过关键步 → antihack_signals 非空。

    task_ctx（dict 形态 critical_steps）+ store 事件（type 键 dict）经
    垫片进 detect——ra 轨迹缺 critical_steps 证据 → skipped_steps 记入。
    """
    seen = {}

    def cap_handler(component, prompt_name, vars, *, temperature=0.0):
        seen.update(vars=dict(vars))
        return json.dumps(_PW, ensure_ascii=False)

    services = make_services(handler=cap_handler)
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        stores = c._transport.app.state.stores
        stores["evalsets"]["es1"] = {
            "tasks": [
                {
                    "id": "t-1",
                    "title": "接口故障定位",
                    "expected": {
                        "critical_steps": [{"evidence": "display interface"}],
                    },
                }
            ]
        }
        _seed_pair(c)  # ra 只跑 display version——缺关键步证据
        h = await admin_headers(c)
        resp = await c.post(
            "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}, headers=h
        )
        assert resp.status_code == 200, resp.text
    sig = seen["vars"]["antihack_signals"]
    assert sig, "antihack 信号通道仍为空串——垫片/注册未生效"
    assert "skipped_steps=" in sig
    assert "display interface" in sig  # ra 缺证据 → 该步记为 skipped
    assert "missing_trial=false" in sig  # status=done 正常透传


# ---------------------------------------------------------------------------
# B2 终审修复回归：dict 路径摘要上限 + 端点权限收紧/自比较拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_side_summary_dict_events_total_capped():
    """B2 终审 Fix2：raw-dict 事件路径无 summarizer budget 兜底——
    超长轨迹拼接后截断到 _SUMMARY_TOTAL_CAP 并带 …(truncated) 标记。"""
    from rsi.judge.llm_judge import _SUMMARY_TOTAL_CAP

    big_output = "x" * 400
    events = [
        {
            "seq": i,
            "type": "tool_call",
            "data": {"command": f"display big-{i}", "output": big_output},
        }
        for i in range(60)  # 60 × ~440 字符 ≈ 26k >> 8000
    ]
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen.update(vars=vars)
        return json.dumps(_PW, ensure_ascii=False)

    judge = LLMJudge(LLMClient(handler=handler))
    await judge.pairwise(
        dict(_TASK), ({"root_cause": "b"}, events), ({"root_cause": "c"}, events)
    )
    for key in ("baseline_summary", "candidate_summary"):
        s = seen["vars"][key]
        assert len(s) <= _SUMMARY_TOTAL_CAP + len("…(truncated)")
        assert s.endswith("…(truncated)")
        assert "display big-0" in s  # 截断保留头部证据


async def test_endpoint_requires_iteration_manage_and_rejects_self_compare(client):
    """B2 终审 Fix3：pairwise 触发 LLM 计费——权限收敛到 iteration:manage
    （同 :re-judge 先例）；自比较（other_run_id == run_id）→ 422。"""
    _seed_pair(client)
    # 无 token → 401
    resp = await client.post(
        "/api/v1/runs/ra/pairwise", json={"other_run_id": "rb"}
    )
    assert resp.status_code == 401, resp.text
    # researcher（仅 view:read，无 iteration:manage）→ 403
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": "researcher", "password": "researcher-pw"},
    )
    assert r.status_code == 200, r.text
    resp = await client.post(
        "/api/v1/runs/ra/pairwise",
        json={"other_run_id": "rb"},
        headers={"Authorization": f"Bearer {r.json()['token']}"},
    )
    assert resp.status_code == 403, resp.text
    # admin 有权限但自比较 → 422（且不消费 LLM：同 run 无对比意义）
    h = await admin_headers(client)
    resp = await client.post(
        "/api/v1/runs/ra/pairwise", json={"other_run_id": "ra"}, headers=h
    )
    assert resp.status_code == 422, resp.text
    assert "distinct" in resp.json()["message"]
