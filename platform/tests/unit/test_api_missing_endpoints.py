"""Missing-endpoint completion tests (interfaces §9, D-2/T2-6/T2-7)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.core.errors import GitStoreError, NotFastForwardError
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.versioning.changeset import FileDiff, LineageGraph, LineageNode

pytestmark = pytest.mark.asyncio


class FakeGitStore:
    """In-memory GitStore double (list/write/delete/snapshot/...)."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.calls: list[tuple] = []

    def list_files(self, sut_id, version=None):
        return dict(self.files)

    def write_file(self, sut_id, path, content, commit=False, message=""):
        self.files[f"suts/{sut_id}/{path}"] = content
        self.calls.append(("write", path, commit, message))

    def delete_file(self, sut_id, path, commit=False, message=""):
        self.files.pop(f"suts/{sut_id}/{path}", None)
        self.calls.append(("delete", path))

    def snapshot(self, sut_id):
        self.calls.append(("snapshot", sut_id))
        return "v-snap-1"

    def promote(self, candidate):
        if candidate == "not-ff":
            raise NotFastForwardError("candidate is not a fast-forward")
        self.calls.append(("promote", candidate))

    def rollback(self, sut_id, version_id):
        if version_id == "missing":
            raise GitStoreError("unknown version")
        self.calls.append(("rollback", sut_id, version_id))

    def lineage(self, sut_id):
        return LineageGraph(
            nodes=[
                LineageNode(
                    version_id="v1", label=f"{sut_id}/v1", kind="baseline"
                ),
                LineageNode(
                    version_id="v2",
                    label=f"{sut_id}/c1",
                    kind="candidate",
                    parent_ids=["v1"],
                ),
            ],
            edges=[("v1", "v2")],
        )

    def diff(self, a, b):
        if a == "nope":
            raise GitStoreError("bad sha")
        return [FileDiff(path="system-prompt.md", op="edit", patch="@@ -1 +1 @@")]


class StubHostService:
    def __init__(self):
        self.destroyed: list[str] = []

    def destroy(self, handle):
        self.destroyed.append(handle.instance_id)


def make_services(**extra) -> dict:
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
            "researcher": {
                "user_id": "u-res",
                "role": "researcher",
                "password_hash": auth.hash_password("res-pw"),
            },
        },
    }
    services.update(extra)
    return services


@pytest_asyncio.fixture
async def client():
    git = FakeGitStore()
    host = StubHostService()
    services = make_services(git_store=git, host_service=host)
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c._git = git
        c._host = host
        yield c


async def h_admin(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def h_res(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "researcher", "password": "res-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def make_sut(client, h) -> str:
    resp = await client.post(
        "/api/v1/suts",
        json={"name": "sut-a", "carrier": "linux", "image_ref": "img:1"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── §9.3 versions diff ─────────────────────────────────────────────────────


class TestVersionsDiff:
    async def test_diff_ok(self, client):
        h = await h_admin(client)
        resp = await client.get("/api/v1/versions/v1/diff/v2", headers=h)
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["path"] == "system-prompt.md"
        assert body[0]["op"] == "edit"
        assert "@@" in body[0]["patch"]

    async def test_diff_unknown_version_404(self, client):
        h = await h_admin(client)
        resp = await client.get("/api/v1/versions/nope/diff/v2", headers=h)
        assert resp.status_code == 404


# ── §9.2 suts versioning + files ───────────────────────────────────────────


class TestSutVersioning:
    async def test_snapshot_201(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(f"/api/v1/suts/{sut_id}/versions:snapshot", headers=h)
        assert resp.status_code == 201
        assert resp.json()["version_id"] == "v-snap-1"

    async def test_snapshot_unknown_sut_404(self, client):
        h = await h_admin(client)
        resp = await client.post("/api/v1/suts/zz/versions:snapshot", headers=h)
        assert resp.status_code == 404

    async def test_promote_204_and_auto_experiment(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            f"/api/v1/suts/{sut_id}/versions:promote",
            json={
                "candidate_version_id": "v2",
                "baseline_batch_id": "batch-b1",
                "candidate_batch_id": "batch-c1",
                "reason": "hit rate up",
            },
            headers=h,
        )
        assert resp.status_code == 204
        exps = await client.get("/api/v1/experiments", headers=h)
        assert any(
            e["decision"] == "promote" for e in exps.json()["items"]
        )

    async def test_promote_not_fast_forward_409(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            f"/api/v1/suts/{sut_id}/versions:promote",
            json={"candidate_version_id": "not-ff"},
            headers=h,
        )
        assert resp.status_code == 409

    async def test_rollback_204_and_404(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            f"/api/v1/suts/{sut_id}/versions:rollback",
            json={"version_id": "v1"},
            headers=h,
        )
        assert resp.status_code == 204
        resp = await client.post(
            f"/api/v1/suts/{sut_id}/versions:rollback",
            json={"version_id": "missing"},
            headers=h,
        )
        assert resp.status_code == 404

    async def test_lineage(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.get(f"/api/v1/suts/{sut_id}/lineage", headers=h)
        assert resp.status_code == 200
        body = resp.json()
        assert {n["kind"] for n in body["nodes"]} == {"baseline", "candidate"}
        assert body["edges"] == [["v1", "v2"]]

    async def test_verify_image_501(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.get(f"/api/v1/suts/{sut_id}/verify-image", headers=h)
        assert resp.status_code == 501
        assert "S11" in resp.json()["message"]


class TestSutFiles:
    async def test_write_staging_then_read(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.put(
            f"/api/v1/suts/{sut_id}/files/system-prompt.md",
            json={"content": "# prompt", "commit": False},
            headers=h,
        )
        assert resp.status_code == 204, resp.text
        # staging 内容可读
        resp = await client.get(f"/api/v1/suts/{sut_id}/files", headers=h)
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert files["system-prompt.md"] == "# prompt"
        # exact path query
        resp = await client.get(
            f"/api/v1/suts/{sut_id}/files?path=system-prompt.md", headers=h
        )
        assert resp.json()["files"]["system-prompt.md"] == "# prompt"

    async def test_commit_true_reaches_git_commit(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.put(
            f"/api/v1/suts/{sut_id}/files/system-prompt.md",
            json={"content": "x", "commit": True, "message": "m"},
            headers=h,
        )
        assert resp.status_code == 204
        assert ("write", "system-prompt.md", True, "m") in client._git.calls

    async def test_frozen_domain_path_409(self, client):
        """关键安全测试：冻结域路径（judge/**）即使 commit=true 也被
        TargetRegistry 拒绝 → 409。"""
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.put(
            f"/api/v1/suts/{sut_id}/files/judge/rubric.md",
            json={"content": "tamper", "commit": True},
            headers=h,
        )
        assert resp.status_code == 409
        assert not any(c[0] == "write" for c in client._git.calls)

    async def test_out_of_domain_path_409(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.put(
            f"/api/v1/suts/{sut_id}/files/random/file.txt",
            json={"content": "x"},
            headers=h,
        )
        assert resp.status_code == 409

    async def test_post_create_201_and_delete_204(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            f"/api/v1/suts/{sut_id}/files",
            json={"path": "skills/foo.md", "content": "bar"},
            headers=h,
        )
        assert resp.status_code == 201
        resp = await client.delete(
            f"/api/v1/suts/{sut_id}/files/skills/foo.md", headers=h
        )
        assert resp.status_code == 204
        assert ("delete", "skills/foo.md") in client._git.calls

    async def test_researcher_forbidden_403(self, client):
        h = await h_res(client)
        sut_id = await make_sut(client, await h_admin(client))
        resp = await client.put(
            f"/api/v1/suts/{sut_id}/files/system-prompt.md",
            json={"content": "x"},
            headers=h,
        )
        assert resp.status_code == 403


# ── §9.10 hypotheses ───────────────────────────────────────────────────────


class TestHypotheses:
    async def test_create_list_roundtrip(self, client):
        h = await h_admin(client)
        resp = await client.post(
            "/api/v1/hypotheses",
            json={"sut_id": "sut-a", "statement": "add verification steps"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        hyp = resp.json()
        assert hyp["status"] == "draft"

        resp = await client.get("/api/v1/hypotheses?sut_id=sut-a", headers=h)
        assert resp.status_code == 200
        assert any(item["id"] == hyp["id"] for item in resp.json()["items"])

    async def test_submit_review_flow(self, client):
        h = await h_admin(client)
        hyp = (
            await client.post(
                "/api/v1/hypotheses",
                json={"sut_id": "sut-a", "statement": "s"},
                headers=h,
            )
        ).json()
        resp = await client.post(
            f"/api/v1/hypotheses/{hyp['id']}:submit-review", headers=h
        )
        assert resp.status_code == 204
        # 已提交再提交 → 409
        resp = await client.post(
            f"/api/v1/hypotheses/{hyp['id']}:submit-review", headers=h
        )
        assert resp.status_code == 409

    async def test_unknown_404(self, client):
        h = await h_admin(client)
        resp = await client.get("/api/v1/hypotheses/zz", headers=h)
        assert resp.status_code == 404


# ── §9.19 credentials ──────────────────────────────────────────────────────


class TestCredentials:
    async def test_create_response_has_no_secret(self, client):
        h = await h_admin(client)
        resp = await client.post(
            "/api/v1/credentials",
            json={"name": "core-sw", "kind": "device_ssh", "secret": "SUPER-SECRET"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        assert "secret" not in resp.json()
        assert "SUPER-SECRET" not in resp.text
        resp = await client.get("/api/v1/credentials", headers=h)
        assert resp.status_code == 200
        assert all("secret" not in c for c in resp.json()["items"])

    async def test_rotate_204_and_unknown_404(self, client):
        h = await h_admin(client)
        cred = (
            await client.post(
                "/api/v1/credentials",
                json={"name": "nms", "kind": "nms_api", "secret": "s1"},
                headers=h,
            )
        ).json()
        resp = await client.post(
            f"/api/v1/credentials/{cred['id']}:rotate",
            json={"secret": "s2"},
            headers=h,
        )
        assert resp.status_code == 204
        resp = await client.post(
            "/api/v1/credentials/zz:rotate", json={"secret": "s"}, headers=h
        )
        assert resp.status_code == 404

    async def test_researcher_forbidden_403(self, client):
        h = await h_res(client)
        resp = await client.post(
            "/api/v1/credentials",
            json={"name": "x", "kind": "llm_api", "secret": "y"},
            headers=h,
        )
        assert resp.status_code == 403


# ── §9.19 users ────────────────────────────────────────────────────────────


class TestUsers:
    async def test_create_login_disable(self, client):
        h = await h_admin(client)
        resp = await client.post(
            "/api/v1/users",
            json={"username": "eng-1", "password": "pw-123", "role": "agent_engineer"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        assert "password" not in resp.json()

        # 新用户可登录
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "eng-1", "password": "pw-123"}
        )
        assert resp.status_code == 200
        token = resp.json()["token"]

        # 禁用后 token 失效（per-request disabled check）
        user_id = resp.json()["user_id"]
        resp = await client.put(
            f"/api/v1/users/{user_id}", json={"disabled": True}, headers=h
        )
        assert resp.status_code == 204
        resp = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401

    async def test_duplicate_username_409(self, client):
        h = await h_admin(client)
        body = {"username": "eng-2", "password": "pw", "role": "researcher"}
        await client.post("/api/v1/users", json=body, headers=h)
        resp = await client.post("/api/v1/users", json=body, headers=h)
        assert resp.status_code == 409

    async def test_researcher_forbidden_403(self, client):
        h = await h_res(client)
        resp = await client.post(
            "/api/v1/users",
            json={"username": "x", "password": "y", "role": "admin"},
            headers=h,
        )
        assert resp.status_code == 403


# ── §9.4 evalsets :assign/:export/:import ─────────────────────────────────


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("env.yaml", "topology: small")
        zf.writestr(
            "tasks.jsonl",
            json.dumps({"id": "t-1", "prompt": "p1"}) + "\n"
            + json.dumps({"id": "t-2", "prompt": "p2"}),
        )
        zf.writestr("configs/extra.yaml", "x: 1")
    return buf.getvalue()


class TestEvalsetsIo:
    async def _make(self, client, h, name="es-io"):
        resp = await client.post(
            "/api/v1/evalsets",
            json={
                "name": name,
                "tasks": [{"id": "t-1", "prompt": "p1"}],
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_assign(self, client):
        h = await h_admin(client)
        es_id = await self._make(client, h)
        resp = await client.post(
            f"/api/v1/evalsets/{es_id}:assign",
            json={"purpose": "holdout"},
            headers=h,
        )
        assert resp.status_code == 204
        assert (await client.get(f"/api/v1/evalsets/{es_id}", headers=h)).json()[
            "purpose"
        ] == "holdout"
        resp = await client.post(
            f"/api/v1/evalsets/{es_id}:assign", json={"purpose": "bogus"}, headers=h
        )
        assert resp.status_code == 422

    async def test_export_zip_and_import_roundtrip(self, client):
        h = await h_admin(client)
        es_id = await self._make(client, h)
        resp = await client.post(f"/api/v1/evalsets/{es_id}:export", headers=h)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "tasks.jsonl" in zf.namelist()

        # import round-trip
        resp = await client.post(
            "/api/v1/evalsets:import",
            files={"file": ("es-io.zip", _zip_bytes(), "application/zip")},
            data={"name": "es-imported"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        imported = resp.json()
        assert imported["name"] == "es-imported"
        detail = await client.get(f"/api/v1/evalsets/{imported['id']}", headers=h)
        tasks = detail.json()["tasks"]
        assert [t["id"] for t in tasks] == ["t-1", "t-2"]

    async def test_import_name_conflict_409(self, client):
        h = await h_admin(client)
        await self._make(client, h, name="dup")
        resp = await client.post(
            "/api/v1/evalsets:import",
            files={"file": ("dup.zip", _zip_bytes(), "application/zip")},
            data={"name": "dup"},
            headers=h,
        )
        assert resp.status_code == 409

    async def test_create_name_conflict_409(self, client):
        h = await h_admin(client)
        await self._make(client, h, name="dup2")
        resp = await client.post(
            "/api/v1/evalsets", json={"name": "dup2", "tasks": []}, headers=h
        )
        assert resp.status_code == 409


# ── §9.6 batches list + :rerun ─────────────────────────────────────────────


class TestBatches:
    async def _batch(self, client, h, evalset_id, sut_id, purpose="iterate"):
        resp = await client.post(
            "/api/v1/batches",
            json={
                "evalset_id": evalset_id,
                "sut_id": sut_id,
                "purpose": purpose,
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_list_with_filters(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            "/api/v1/evalsets", json={"name": "es-b", "tasks": []}, headers=h
        )
        es_id = resp.json()["id"]
        b = await self._batch(client, h, es_id, sut_id)
        resp = await client.get("/api/v1/batches", headers=h)
        assert resp.status_code == 200
        assert any(item["id"] == b["id"] for item in resp.json()["items"])
        resp = await client.get(
            f"/api/v1/batches?sut_id={sut_id}&status={b['status']}", headers=h
        )
        assert [item["id"] for item in resp.json()["items"]] == [b["id"]]
        resp = await client.get("/api/v1/batches?sut_id=nope", headers=h)
        assert resp.json()["items"] == []

    async def test_rerun_creates_bypass_batch(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            "/api/v1/evalsets", json={"name": "es-r", "tasks": []}, headers=h
        )
        es_id = resp.json()["id"]
        b = await self._batch(client, h, es_id, sut_id)
        client._transport.app.state.stores["batches"][b["id"]]["status"] = "failed"
        resp = await client.post(
            f"/api/v1/batches/{b['id']}:rerun",
            json={"scope": "failed", "bypass_cache": True},
            headers=h,
        )
        assert resp.status_code == 204, resp.text
        listing = await client.get("/api/v1/batches", headers=h)
        rerun = [
            x for x in listing.json()["items"] if x["id"] != b["id"]
        ]
        assert len(rerun) == 1

    async def test_rerun_running_409(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await client.post(
            "/api/v1/evalsets", json={"name": "es-r2", "tasks": []}, headers=h
        )
        b = await self._batch(client, h, resp.json()["id"], sut_id)
        resp = await client.post(
            f"/api/v1/batches/{b['id']}:rerun",
            json={"scope": "all"},
            headers=h,
        )
        assert resp.status_code == 409


# ── §9.13 labs snapshots/restore/readiness/exec ───────────────────────────


class TestLabsOps:
    async def _lab(self, client, h):
        resp = await client.post(
            "/api/v1/labs",
            json={"sut_id": "sut-x", "topology": {}},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_snapshot_restore(self, client):
        h = await h_admin(client)
        lab_id = await self._lab(client, h)
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/snapshots", json={"name": "s0"}, headers=h
        )
        assert resp.status_code == 201, resp.text
        snap_id = resp.json()["snapshot_id"]
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/restore",
            json={"snapshot_id": snap_id},
            headers=h,
        )
        assert resp.status_code == 204
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/restore",
            json={"snapshot_id": "nope"},
            headers=h,
        )
        assert resp.status_code == 404

    async def test_readiness(self, client):
        h = await h_admin(client)
        lab_id = await self._lab(client, h)
        resp = await client.get(
            f"/api/v1/labs/{lab_id}/devices/sut/readiness", headers=h
        )
        assert resp.status_code == 200
        assert resp.json()["ready"] is True
        resp = await client.get(
            f"/api/v1/labs/{lab_id}/devices/nosuch/readiness", headers=h
        )
        assert resp.status_code == 404

    async def test_exec_whitelist(self, client):
        h = await h_admin(client)
        lab_id = await self._lab(client, h)
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/devices/sut/exec",
            json={"cli": "display version"},
            headers=h,
        )
        assert resp.status_code == 200
        assert "display version" in resp.text
        # 白名单外 → 409
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/devices/sut/exec",
            json={"cli": "reload now"},
            headers=h,
        )
        assert resp.status_code == 409
        # 未知设备 → 404
        resp = await client.post(
            f"/api/v1/labs/{lab_id}/devices/nosuch/exec",
            json={"cli": "display version"},
            headers=h,
        )
        assert resp.status_code == 404


# ── §9.12 iterations mode / 门② / 互斥 ─────────────────────────────────────


class TestIterationsContract:
    async def _make(self, client, h, mode="p1", sut_id=None):
        if sut_id is None:
            sut_id = await make_sut(client, h)
        return (
            await client.post(
                "/api/v1/iterations",
                json={"sut_id": sut_id, "profile_id": "prof-1", "mode": mode},
                headers=h,
            )
        )

    async def test_mode_p2_409(self, client):
        h = await h_admin(client)
        resp = await self._make(client, h, mode="p2")
        assert resp.status_code == 409
        assert "P2" in resp.json()["message"]

    async def test_mode_invalid_422(self, client):
        h = await h_admin(client)
        resp = await self._make(client, h, mode="bogus")
        assert resp.status_code == 422

    async def test_same_sut_active_409(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        resp = await self._make(client, h, sut_id=sut_id)
        assert resp.status_code == 201
        resp = await self._make(client, h, sut_id=sut_id)
        assert resp.status_code == 409

    async def test_unknown_sut_404(self, client):
        h = await h_admin(client)
        resp = await self._make(client, h, sut_id="nope")
        assert resp.status_code == 404

    async def test_gate2_hypothesis_201(self, client):
        h = await h_admin(client)
        sut_id = await make_sut(client, h)
        it_id = (await self._make(client, h, sut_id=sut_id)).json()[
            "iteration_id"
        ]
        await client.post(f"/api/v1/iterations/{it_id}:pause", headers=h)
        resp = await client.post(
            f"/api/v1/iterations/{it_id}/hypothesis",
            json={"hypothesis": "tighten tool timeouts"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        hyp_id = resp.json()["hypothesis_id"]
        assert hyp_id
        hyp = await client.get(f"/api/v1/hypotheses/{hyp_id}", headers=h)
        assert hyp.status_code == 200
        assert hyp.json()["iteration_id"] == it_id


# ── §9.14 instances destroy wiring ─────────────────────────────────────────


class TestInstanceDestroy:
    async def test_delete_calls_host_destroy(self, client):
        h = await h_admin(client)
        app_stores = client._transport.app.state.stores
        app_stores["instances"]["inst-1"] = {
            "id": "inst-1",
            "sut_id": "sut-x",
            "lab_id": None,
            "status": "running",
            "run_id": "run-1",
            "host": "h1",
        }
        resp = await client.delete("/api/v1/instances/inst-1", headers=h)
        assert resp.status_code == 204
        assert client._host.destroyed == ["inst-1"]
        assert "inst-1" not in app_stores["instances"]

    async def test_delete_unknown_404(self, client):
        h = await h_admin(client)
        resp = await client.delete("/api/v1/instances/zz", headers=h)
        assert resp.status_code == 404


# ── §9.11 sft-report + §9.17 noise-bands ──────────────────────────────────


class TestReports:
    async def test_sft_report_markdown(self, client):
        h = await h_admin(client)
        stores = client._transport.app.state.stores
        stores["experiments"]["exp-1"] = {
            "id": "exp-1",
            "sut_id": "sut-a",
            "iteration_id": None,
            "versions": {},
            "decision": "revert",
            "evidence": [],
        }
        resp = await client.get("/api/v1/experiments/exp-1/sft-report", headers=h)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert "SFT" in resp.text

    async def test_sft_report_unknown_404(self, client):
        h = await h_admin(client)
        resp = await client.get("/api/v1/experiments/zz/sft-report", headers=h)
        assert resp.status_code == 404

    async def test_noise_bands(self, client):
        h = await h_res(client)

        class FakeCalibrator:
            factor = 1.96
            last_sigma = {"success_rate": 0.05}
            last_n_pairs = 3
            last_calibrated_at = "2026-11-05T00:00:00Z"

        client._transport.app.state.services["noise_band"] = FakeCalibrator()
        resp = await client.get("/api/v1/dashboard/noise-bands", headers=h)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["metric"] == "success_rate"
        assert items[0]["sigma"] == pytest.approx(0.05)
        assert items[0]["delta"] == pytest.approx(1.96 * 0.05)
        assert items[0]["valid"] is True


# ── §9.20 WS token auth ────────────────────────────────────────────────────


class TestWsToken:
    def test_ws_rejects_missing_token(self):
        from rsi.core.errors import AuthError  # noqa: F401  (readability)

        services = make_services()
        app = create_app(services)
        with TestClient(app) as tc:
            with pytest.raises(Exception):
                with tc.websocket_connect("/api/v1/streams/runs/run-1"):
                    pass

    def test_ws_accepts_valid_token(self):
        services = make_services()
        app = create_app(services)
        app.state.stores["runs"]["run-1"] = {
            "id": "run-1", "batch_id": "b", "status": "done",
        }
        auth = services["auth_service"]
        rec = services["users"]["admin"]
        token, _ = auth.login(
            rec["user_id"], rec["role"], rec["password_hash"], "admin-pw"
        )
        with TestClient(app) as tc:
            with tc.websocket_connect(
                f"/api/v1/streams/runs/run-1?token={token}"
            ) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "tail"
