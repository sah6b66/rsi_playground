"""B2-T4：LLM 假设起草——propose.v1 渲染、schema 校验、LLM 未配/失败 503."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.llm.client import LLMClient
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio

_DRAFT = {
    "failure_type": "missed_critical_command",
    "defect_class": "A",
    "proposal_type": "modify_prompt",
    "diff_content": "--- a/system-prompt.md\n+++ b/system-prompt.md\n@@\n"
    "+步骤3 必须执行 display interface brief",
    "predicted_affected_tasks": ["task-3", "task-7"],
}

# propose.v1 模板的四个占位符——端点 vars 必须齐备（缺 var → render ConfigError）
_REQUIRED_VARS = {
    "problem_statement",
    "element_texts",
    "enabled_targets",
    "failed_hypotheses",
}


def make_services(llm) -> dict:
    """复制 make_services 模式（tests/unit/test_api_core.py:26），附加 llm_client."""
    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
        },
        "llm_client": llm,
    }
    return services


async def _hdr(client) -> dict:
    r = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_draft_returns_structured_hypothesis(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["component"] = component
        seen["prompt_name"] = prompt_name
        seen["vars"] = vars
        return json.dumps(_DRAFT, ensure_ascii=False)

    app = create_app(make_services(LLMClient(handler=handler)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/iterations/iter-1/hypotheses:draft",
            headers=await _hdr(c),
            json={
                "problem_statement": "漏查接口状态",
                "element_texts": {"system-prompt.md": "原文…"},
                "enabled_targets": ["modify_prompt"],
                "failed_hypotheses": [],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()["draft"]
    assert body["proposal_type"] == "modify_prompt"
    assert body["failure_type"] == "missed_critical_command"
    assert body["defect_class"] == "A"
    assert body["predicted_affected_tasks"] == ["task-3", "task-7"]
    # FR6.3：调的是 hypothesis/propose 提示，vars 四键齐备（缺键 render 抛 ConfigError）
    assert seen["component"] == "hypothesis"
    assert seen["prompt_name"] == "propose"
    assert _REQUIRED_VARS <= set(seen["vars"])
    assert seen["vars"]["problem_statement"] == "漏查接口状态"
    assert "system-prompt.md" in seen["vars"]["element_texts"]
    assert "modify_prompt" in seen["vars"]["enabled_targets"]


async def test_draft_503_when_llm_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    app = create_app(make_services(None))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/iterations/iter-1/hypotheses:draft",
            headers=await _hdr(c),
            json={
                "problem_statement": "x",
                "element_texts": {},
                "enabled_targets": [],
                "failed_hypotheses": [],
            },
        )
    assert resp.status_code == 503
    assert resp.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_draft_caps_element_texts(tmp_path, monkeypatch):
    """element_texts 逐条 content[:1500] 多行拼接，总长 cap 8000。"""
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["vars"] = vars
        return json.dumps(_DRAFT, ensure_ascii=False)

    big = "x" * 5000
    app = create_app(make_services(LLMClient(handler=handler)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/iterations/iter-1/hypotheses:draft",
            headers=await _hdr(c),
            json={
                "problem_statement": "p",
                "element_texts": {f"f{i}.md": big for i in range(10)},
                "enabled_targets": [],
                "failed_hypotheses": [],
            },
        )
    assert resp.status_code == 200, resp.text
    et = seen["vars"]["element_texts"]
    assert len(et) <= 8000
    lines = et.split("\n")
    assert lines  # 至少装下一条
    # 每行 = "path: content[:1500]"，超长内容被截断
    for line in lines:
        path, content = line.split(": ", 1)
        assert len(content) <= 1500


async def test_draft_invalid_llm_output_503(tmp_path, monkeypatch):
    """LLM 输出反复不过 schema → LLMOutputInvalid → 503 且消息含摘要。"""
    monkeypatch.setenv("RSI_HOME", str(tmp_path))

    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "这不是 JSON"

    app = create_app(make_services(LLMClient(handler=handler)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/iterations/iter-1/hypotheses:draft",
            headers=await _hdr(c),
            json={
                "problem_statement": "p",
                "element_texts": {},
                "enabled_targets": [],
                "failed_hypotheses": [],
            },
        )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert "propose" in body["message"] or "schema" in body["message"]
