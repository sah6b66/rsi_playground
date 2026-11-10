import pytest
from fastapi import HTTPException

from rsi.security.rbac import ROLE_PERMISSIONS, require

ALL_PERMISSIONS = {
    "sut:manage",
    "evalset:manage",
    "profile:manage",
    "batch:trigger",
    "iteration:manage",
    "version:promote_rollback",
    "approval:decide",
    "credential:manage",
    "user:manage",
    "system:stop",
    "view:read",
    "image:manage",
    "sandbox:manage",
    "env:manage",
}


class TestRbacMatrix:
    def test_admin_has_all(self):
        assert ROLE_PERMISSIONS["admin"] == ALL_PERMISSIONS
        assert "sut:manage" in ROLE_PERMISSIONS["admin"]
        assert "credential:manage" in ROLE_PERMISSIONS["admin"]
        assert "approval:decide" in ROLE_PERMISSIONS["admin"]

    def test_engineer_no_credentials(self):
        assert "credential:manage" not in ROLE_PERMISSIONS["agent_engineer"]
        assert ROLE_PERMISSIONS["agent_engineer"] == {
            "sut:manage",
            "evalset:manage",
            "profile:manage",
            "batch:trigger",
            "iteration:manage",
            "version:promote_rollback",
            "view:read",
        }

    def test_iteration_manage_not_on_service_or_researcher(self):
        # audit4 A-14: iteration lifecycle (create/pause/resume/abort/
        # archive, re-judge) is admin/agent_engineer only — the service
        # automation role must not drive human-gated iterations.
        for role in ("service", "researcher", "approver"):
            assert "iteration:manage" not in ROLE_PERMISSIONS[role]
        assert "iteration:manage" in ROLE_PERMISSIONS["admin"]
        assert "iteration:manage" in ROLE_PERMISSIONS["agent_engineer"]
        dep = require("iteration:manage")
        with pytest.raises(HTTPException) as e:
            dep(FakeUser("service"))
        assert e.value.status_code == 403

    def test_researcher_readonly(self):
        perms = ROLE_PERMISSIONS["researcher"]
        assert "view:read" in perms
        assert "sut:manage" not in perms

    def test_approver_and_service(self):
        assert ROLE_PERMISSIONS["approver"] == {"view:read", "approval:decide"}
        assert ROLE_PERMISSIONS["service"] == {"batch:trigger", "view:read"}


class FakeUser:
    def __init__(self, role):
        self.role = role


class TestRequire:
    def test_allowed(self):
        dep = require("sut:manage")
        assert dep(FakeUser("admin")) is not None

    def test_denied(self):
        dep = require("credential:manage")
        with pytest.raises(HTTPException) as e:
            dep(FakeUser("agent_engineer"))
        assert e.value.status_code == 403

    def test_multi_perm_all_required(self):
        dep = require("batch:trigger", "view:read")
        assert dep(FakeUser("service")) is not None
        with pytest.raises(HTTPException):
            dep(FakeUser("researcher"))

    def test_unknown_role_denied(self):
        dep = require("view:read")
        with pytest.raises(HTTPException):
            dep(FakeUser("ghost"))
