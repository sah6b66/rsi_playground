# P1 B1 打底批 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 P0 遗留四项——evalset 变更持久化、suts/evalsets DB 行同步、batches/runs 落库 FK 真实 ID、doctor 契约文档化、LLM 传输层退避重试 + backup_provider + 真实 usage 回填——为 P1 后续批次（B2 LLM 主线起）提供数据可信与 LLM 健壮性地基。

**Architecture:** 在既有对象文件库（`rsi/api/object_store.py`，JSON 落盘 + 启动水合）之上补齐写路径缺口并同步 DB 存在性行；FK 修复走引擎 `db_meta` 参数线程化（API 路径已正确）；LLM 健壮化按 DD-02 §5.1 伪码在 provider handler 层实现重试/备链/usage 透传，`LLMClient` 协议向后兼容（str 或 `(str, usage)` 返回）。

**Tech Stack:** Python 3.12 + uv + FastAPI + SQLModel/SQLite + httpx；测试 pytest（全离线，LLM 用注入 handler）。

**Spec:** `docs/superpowers/specs/2026-11-16-p1-phase-design.md` §4 B1（含 2026-11-16 修订的现状核实结论）

## Global Constraints

- 工作目录 `platform/`；回归命令 `uv run pytest -q`，基线 **1453 passed + 12 skipped**，每 task 完成后不得低于基线（新增用例计入）
- 全离线测试原则：不依赖 Docker/网络/真实 LLM；LLM 行为用注入 handler 测试
- DB 写入一律 try/except + warning 降级，不得阻断主流程（项目铁律）
- 文档权威：DD-02 §5.1（重试/备胎伪码，`docs/detail_design/RSI-PG-DD-02-llm.md:93-115`）；DB suts/evalsets 表语义="存在性证明"（`resolver.py:11-14` 注释），payload 不入 DB
- 命名约定：Python 模块 `ensp_pro.py`（下划线），配置键/env.yaml 枚举 `ensp-pro`（连字符）——有意区分，勿"纠正"
- Pyright 报 `rsi.*` import 无法解析是 IDE 噪音，一律忽略
- commit 风格参照 `git log`：`feat(scope)/fix(scope): 中文主题`；每 task 一个 commit
- `persist_record(services, collection, record)`：`rsi/api/object_store.py:103-105`；`delete_record` 同文件；`PERSISTED_COLLECTIONS`（object_store.py:26-27）
- 测试夹具模式：每文件自带 `make_services()`（参照 `tests/unit/test_api_core.py:26`）+ `create_app` + `ASGITransport/AsyncClient` + `RSI_HOME` monkeypatch + admin 登录 helper（参照 `tests/unit/test_web_resources_api.py:92-130`）

---

### Task 1: evalset 变更端点持久化补齐

**Files:**
- Modify: `platform/rsi/api/routers/evalsets.py:253-272`（update_evalset）、`:291-320`（freeze_evalset）、`:350-358`（assign_evalset）
- Test: `platform/tests/unit/test_evalset_persist_gaps.py`（新建）

**Interfaces:**
- Consumes: `persist_record(services, collection, record)`（`rsi/api/object_store.py:103`，已存在）
- Produces: 无下游依赖（独立修复；Task 2 会扩展 persist_record 行为但签名不变）

- [ ] **Step 1: 写失败测试**

```python
"""B1-1：evalset 变更端点（update/:freeze/:assign）落盘缺口.

F3 只覆盖了 create/:import/:clone/delete；update（version 自增）、
freeze、assign 改记录后不 persist——重启即回退。断言三端点变更后
JSON 对象库文件内容与内存一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.settings import Settings

pytestmark = pytest.mark.asyncio


def make_services(extra: dict | None = None) -> dict:
    # 复制自 tests/unit/test_api_core.py:26（本仓模式：每测试文件自带）
    from rsi.api.deps import default_stores
    from rsi.security.auth import AuthService

    settings = Settings()
    services = default_stores(settings)
    services["auth"] = AuthService()
    services["users"] = {
        "admin": {
            "user_id": "u-admin",
            "role": "admin",
            "password_hash": services["auth"].hash_password("admin-pw"),
        }
    }
    services.update(extra or {})
    return services


def _seed(store: dict) -> None:
    store["es-1"] = {
        "id": "es-1", "name": "ensp-spineleaf", "purpose": "iterate",
        "tasks": [], "description": "d0", "env_yaml": "", "configs": {},
        "frozen": False, "version": 1, "created_at": "2026-01-01T00:00:00Z",
        "versions": [],
    }


def _disk(tmp_path: Path, es_id: str = "es-1") -> dict:
    f = tmp_path / "storage" / "object-store" / "evalsets" / f"{es_id}.json"
    return json.loads(f.read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    services = make_services({"settings": Settings()})
    app = create_app(services)
    _seed(app.state.stores["evalsets"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


async def _hdr(client):
    r = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_update_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.put(
        "/api/v1/evalsets/es-1", json={"description": "d1"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert _disk(tmp_path)["description"] == "d1"
    assert _disk(tmp_path)["version"] == 2


async def test_freeze_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.post("/api/v1/evalsets/es-1:freeze", headers=h)
    assert r.status_code == 200, r.text
    assert _disk(tmp_path)["frozen"] is True


async def test_assign_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.post(
        "/api/v1/evalsets/es-1:assign", json={"purpose": "holdout"}, headers=h
    )
    assert r.status_code == 204, r.text
    assert _disk(tmp_path)["purpose"] == "holdout"
```

注：路由前缀以 `evalsets.py` 顶部 `router = APIRouter(prefix=...)` 实际值为准（若为 `/evalsets` 无 `/api/v1`，按 `create_app` 挂载前缀调整请求路径——先读 `tests/unit/test_api_core.py` 中任一 evalset 请求的写法对齐）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_evalset_persist_gaps.py -v`
Expected: 3 FAIL（`FileNotFoundError`——object-store 下无 JSON 文件）

- [ ] **Step 3: 最小实现**

三处端点各在记录变更后补 persist（需给 `update_evalset`/`freeze_evalset`/`assign_evalset` 增加 `request: Request` 参数并加入依赖注入）：

```python
# update_evalset（evalsets.py:253-272）——签名加 request: Request
    updates = body.model_dump(exclude_unset=True)
    if updates:
        es.update(updates)
        es["version"] += 1
        from rsi.api.object_store import persist_record

        persist_record(request.app.state.services, "evalsets", es)
    return _out(es)

# freeze_evalset——es["frozen"] = True（及其它冻结字段赋值）之后：
    from rsi.api.object_store import persist_record

    persist_record(request.app.state.services, "evalsets", es)

# assign_evalset——es["purpose"] = body.purpose 之后：
    from rsi.api.object_store import persist_record

    persist_record(request.app.state.services, "evalsets", es)
```

（freeze 的冻结字段赋值以现实现为准——先读 `evalsets.py:291-320` 完整函数体，persist 放在所有变更之后、return 之前。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd platform && uv run pytest tests/unit/test_evalset_persist_gaps.py -v`
Expected: 3 PASS

- [ ] **Step 5: 全量回归 + commit**

Run: `cd platform && uv run pytest -q`
Expected: ≥1456 passed + 12 skipped（基线 + 新增 3）

```bash
git add platform/rsi/api/routers/evalsets.py platform/tests/unit/test_evalset_persist_gaps.py
git commit -m "fix(api): B1-1 evalset update/freeze/assign 补对象库持久化（重启不回退）"
```

---

### Task 2: persist_record/delete_record 同步 DB 行（suts/evalsets）

**Files:**
- Modify: `platform/rsi/api/object_store.py`（persist_record:103、delete_record 附近，新增 `_upsert_db_row`/`_remove_db_row`）
- Test: `platform/tests/unit/test_object_store.py`（追加）

**Interfaces:**
- Consumes: `services.get("session_factory")`（bootstrap 装配的真实键；测试 services 无此键 → no-op 降级）；`rsi.db.tables.Sut`（id/name UNIQUE/carrier CHECK/image_ref/description/created_at，tables.py:116-133）、`rsi.db.tables.EvalSet`（id/name UNIQUE/purpose CHECK 4 值/git_ref/content_hash/frozen_at/created_at，tables.py:157-174）
- Produces: persist_record/delete_record 签名不变；副作用=每次持久化同步 upsert DB 行（外键引用完整，B4 join 前置）

- [ ] **Step 1: 写失败测试**（追加到 `tests/unit/test_object_store.py`）

```python
def _sf(tmp_path):
    import sqlite3

    from sqlmodel import SQLModel, Session, create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def test_persist_record_upserts_sut_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "sut-1", "name": "n1", "carrier": "opencode",
           "image_ref": "img:1", "description": "", "created_at": "2026-01-01"}
    persist_record(services, "suts", rec)
    from sqlmodel import select

    from rsi.db.tables import Sut

    with sf() as s:
        assert s.exec(select(Sut)).one().name == "n1"
    rec["name"] = "n2"
    persist_record(services, "suts", rec)  # upsert 不重复插行
    with sf() as s:
        rows = s.exec(select(Sut)).all()
        assert len(rows) == 1 and rows[0].name == "n2"


def test_persist_record_upserts_evalset_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "es-1", "name": "e1", "purpose": "iterate",
           "created_at": "2026-01-01"}
    persist_record(services, "evalsets", rec)
    from sqlmodel import select

    from rsi.db.tables import EvalSet

    with sf() as s:
        assert s.exec(select(EvalSet)).one().purpose == "iterate"


def test_persist_record_degrades_without_session_factory(tmp_path):
    st = ObjectFileStore(tmp_path)
    services = {"object_store": st}
    persist_record(services, "suts", {"id": "sut-1", "name": "n",
                                      "carrier": "opencode", "image_ref": "i",
                                      "description": "", "created_at": "x"})
    assert st.load_all("suts")[0]["name"] == "n"  # JSON 落盘不受影响


def test_delete_record_removes_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "es-1", "name": "e1", "purpose": "iterate",
           "created_at": "2026-01-01"}
    persist_record(services, "evalsets", rec)
    delete_record(services, "evalsets", "es-1")
    from sqlmodel import select

    from rsi.db.tables import EvalSet

    with sf() as s:
        assert s.exec(select(EvalSet)).all() == []
```

（文件头 import 区补 `from rsi.api.object_store import ObjectFileStore, hydrate_object_stores, persist_record, delete_record`——以现有 import 行实际内容为准增删。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_object_store.py -v`
Expected: 新增 4 FAIL（DB 行不存在/无 upsert 行为）

- [ ] **Step 3: 最小实现**（object_store.py，persist_record/delete_record 内追加 DB 同步）

```python
import logging

logger = logging.getLogger(__name__)


def _upsert_db_row(services: dict, collection: str, record: dict) -> None:
    """Best-effort DB 存在性行同步（失败 warning 不阻断——项目 DB 写入铁律）."""
    if collection not in ("suts", "evalsets"):
        return
    sf = services.get("session_factory")
    if sf is None:
        return
    try:
        from sqlmodel import select

        if collection == "suts":
            from rsi.db.tables import Sut as _T
        else:
            from rsi.db.tables import EvalSet as _T
        with sf() as session:
            row = session.exec(
                select(_T).where(_T.id == record["id"])
            ).first()
            if row is None:
                if collection == "suts":
                    row = _T(
                        id=record["id"],
                        name=record.get("name", record["id"]),
                        carrier=record.get("carrier", "opencode"),
                        image_ref=record.get("image_ref", ""),
                        description=record.get("description", ""),
                    )
                else:
                    row = _T(
                        id=record["id"],
                        name=record.get("name", record["id"]),
                        purpose=record.get("purpose", "iterate"),
                    )
                session.add(row)
            else:
                row.name = record.get("name", row.name)
                if collection == "suts":
                    row.image_ref = record.get("image_ref", row.image_ref)
                    row.description = record.get("description", row.description)
                else:
                    row.purpose = record.get("purpose", row.purpose)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "object_store.db_upsert_failed",
            extra={"collection": collection, "id": record.get("id"),
                   "error": str(exc)},
        )


def _remove_db_row(services: dict, collection: str, record_id: str) -> None:
    if collection not in ("suts", "evalsets"):
        return
    sf = services.get("session_factory")
    if sf is None:
        return
    try:
        from sqlmodel import select

        if collection == "suts":
            from rsi.db.tables import Sut as _T
        else:
            from rsi.db.tables import EvalSet as _T
        with sf() as session:
            row = session.exec(select(_T).where(_T.id == record_id)).first()
            if row is not None:
                session.delete(row)
                session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "object_store.db_delete_failed",
            extra={"collection": collection, "id": record_id,
                   "error": str(exc)},
        )
```

persist_record 末尾追加 `_upsert_db_row(services, collection, record)`；delete_record 末尾追加 `_remove_db_row(services, collection, record_id)`。

注意：object_store.py 现有 logger 的调用风格若是 structlog 风格（`logger.warning("event", key=val)` 而非 extra=），以文件内现有写法为准对齐。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd platform && uv run pytest tests/unit/test_object_store.py -v`
Expected: 全 PASS（旧 4 + 新 4）

- [ ] **Step 5: 全量回归 + commit**

Run: `cd platform && uv run pytest -q`
Expected: ≥1460 passed + 12 skipped

```bash
git add platform/rsi/api/object_store.py platform/tests/unit/test_object_store.py
git commit -m "feat(api): B1-2 对象库持久化同步 suts/evalsets DB 行（FK 引用完整）"
```

---

### Task 3: 引擎 db_meta 补 evalset_id + 空串守卫

**Files:**
- Modify: `platform/rsi/orchestrator/iteration.py:480`（iterate_once 签名）、`:627`（_iterate_body 签名）、`:815`（db_meta 构造）
- Modify: `platform/rsi/orchestrator/batch.py:1181` 附近（_db_insert_batch 空串 warning）
- Test: `platform/tests/unit/test_iteration_fk_meta.py`（新建）

**Interfaces:**
- Consumes: `_db_insert_batch`（batch.py:1154-1195，`meta.get("evalset_id","")` 兜底）；`run_batch(name, env, tasks, config_version_id, image_ref, *, db_meta=...)`（引擎侧调用形态，iteration.py:816-829）
- Produces: `IterationEngine.iterate_once(..., evalset_id: str = "")` 与 `_iterate_body(..., evalset_id: str = "")` 新可选参数（默认空串向后兼容既有调用/测试）；db_meta 含 `evalset_id` 键

- [ ] **Step 1: 写失败测试**

```python
"""B1-3：引擎 VERIFYING 批次 db_meta 携带真实 evalset_id（FK 完整性）.

根因：iteration.py:815 db_meta 只带 iteration_id/sut_id，
_db_insert_batch 空串兜底 → batches.evalset_id=""，join 断链。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from rsi.orchestrator.iteration import IterationEngine


class _Bus:
    async def publish(self, *a, **k):
        return None


class _Runner:
    """捕获 run_batch 的 db_meta；返回最小 BatchStats 形状。"""

    def __init__(self):
        self.metas: list[dict] = []

    async def run_batch(self, name, env, tasks, cfg, image_ref, *, db_meta=None):
        self.metas.append(dict(db_meta or {}))
        return SimpleNamespace(
            task_scores=[], metrics={}, succeeded=0, failed=0,
            short_circuit=None,
        )


def _engine(runner):
    return IterationEngine(
        runner,
        SimpleNamespace(analyze=lambda *a, **k: SimpleNamespace(
            clusters=[], directions=[], defect_counts={})),
        SimpleNamespace(decide=lambda ctx: SimpleNamespace(
            action="HOLD", rule_hit=0, reason="stub", evidence={})),
        _Bus(),
        SimpleNamespace(remaining=lambda: True, consume=lambda *a: None),
    )


@pytest.mark.asyncio
async def test_iterate_body_carries_evalset_id_in_db_meta():
    runner = _Runner()
    eng = _engine(runner)
    await eng._iterate_body(
        "iter-1", None, "sut-1",
        SimpleNamespace(enabled_targets=[], goals={}, guards={}),
        None, [], "cfg-1", "img:1", None,
        failed_runs=[], sut_config={}, opt_history=[],
        hypothesis=None, candidate_config_version_id=None,
        noise_delta=None,
        evalset_id="es-1",
    )
    assert runner.metas, "engine should have triggered at least one batch"
    for m in runner.metas:
        assert m.get("evalset_id") == "es-1"
        assert m.get("sut_id") == "sut-1"
        assert m.get("iteration_id") == "iter-1"
```

注：`_iterate_body` 全流程涉及门/裁判/选择器桩——若上述最小桩不足以跑通（如缺 profile 字段/决策记录器），以跑通为度补 SimpleNamespace 属性，**核心断言不变**：所有 run_batch 的 db_meta 含 evalset_id。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_iteration_fk_meta.py -v`
Expected: FAIL（`TypeError: unexpected keyword argument 'evalset_id'` 或 db_meta 无 evalset_id）

- [ ] **Step 3: 最小实现**

```python
# iteration.py:480 iterate_once 签名追加（放在既有可选参数区）：
        evalset_id: str = "",

# iterate_once 内部把 _iterate_body(...) 调用点透传 evalset_id=evalset_id

# iteration.py:627 _iterate_body 签名追加（keyword-only 区）：
        *,
        evalset_id: str = "",

# iteration.py:812-815 db_meta 构造改为：
        db_meta = {
            "iteration_id": iteration_id,
            "sut_id": sut_id,
            "evalset_id": evalset_id,
        }
```

batch.py `_db_insert_batch`（:1181 附近）空串守卫：

```python
        evalset_id = str(meta.get("evalset_id", ""))
        if not evalset_id:
            logger.warning(
                "batch.db_meta_missing_evalset_id",
                batch_id=batch_id,
                hint="caller should thread the real evalset_id via db_meta",
            )
```

（`session.add(Batch(...))` 处改用该局部变量。）

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd platform && uv run pytest tests/unit/test_iteration_fk_meta.py tests/unit/test_batch_runner.py tests/integration/test_closed_loop.py -v`
Expected: 全 PASS（closed_loop 驱动引擎全流程，验证签名向后兼容）
Run: `cd platform && uv run pytest -q`
Expected: ≥1461 passed + 12 skipped

- [ ] **Step 5: Commit**

```bash
git add platform/rsi/orchestrator/iteration.py platform/rsi/orchestrator/batch.py platform/tests/unit/test_iteration_fk_meta.py
git commit -m "fix(orchestrator): B1-3 引擎 db_meta 补 evalset_id——batches 落库 FK 真实 ID"
```

---

### Task 4: doctor 契约文档化（DD-13 补段 + 实现措辞核对）

**Files:**
- Modify: `docs/detail_design/RSI-PG-DD-13-api.md`（新增 /system/doctor 契约小节，放在既有端点契约区末尾）
- Modify（如需）: `platform/rsi/doctor.py`（仅措辞/字段名对齐，不改结构）
- Test: 无新增（`tests/unit/test_doctor.py` 既有覆盖；本 task 以文档为主）

**Interfaces:**
- Consumes: `CheckResult{category,name,status,message,fix_hint}` / `DoctorReport{results,generated_at,hostname,computed{passed,warned,failed,has_errors}}`（doctor.py:27-57 实现即契约源）
- Produces: DD-13 doctor 契约段（B4+ 看板/依赖面板引用的权威措辞）

- [ ] **Step 1: 通读 doctor.py 与上游表述**

读 `platform/rsi/doctor.py` 全文 + `docs/_rsi_playground_requirements.md` FR10.9②（"组件级健康检查（DB/Git 仓库/trace-collector/各仿真后端/冻结校验，统一 health 端点）"）+ 设计文档 doctor 相关段（grep "doctor" docs/_rsi_playground_design.md）。列出三处措辞差异清单（字段名/检查项命名/category 枚举）。

- [ ] **Step 2: DD-13 补契约段**

在 DD-13 适当小节后追加（措辞与 doctor.py 实现一致，格式仿该文件既有端点小节）：

```markdown
### /system/doctor（自检报告，FR10.9②）

`GET /system/doctor` → `DoctorReport`：

| 字段 | 类型 | 说明 |
|------|------|------|
| results | CheckResult[] | 逐项检查结果 |
| generated_at / hostname | str | 报告时间与主机 |
| computed.passed / warned / failed | int | 计数 |
| computed.has_errors | bool | failed > 0 |

CheckResult：`category ∈ {runtime, config, external, internal, security}`、
`name`（检查项标识）、`status ∈ {pass, warn, fail}`、`message`、`fix_hint`。
检查面：DB / Git 裸仓 / trace-collector / 各仿真后端 / 冻结校验 /
LLM provider 配置 / 子网与 Docker 等环境依赖（以实现清单为准滚动）。
```

- [ ] **Step 3: 实现侧对齐（仅当差异清单非空）**

对 Step 1 清单中"实现与上游措辞不一致"的项做最小调整（字段名/消息措辞），跑 `uv run pytest tests/unit/test_doctor.py -v` 保持全 PASS；若调整了响应字段名则同步更新 test_doctor.py 断言。

- [ ] **Step 4: Commit**

```bash
git add docs/detail_design/RSI-PG-DD-13-api.md platform/rsi/doctor.py platform/tests/unit/test_doctor.py
git commit -m "docs(dd13): B1-4 补 /system/doctor 响应契约段 + 实现措辞对齐"
```

---

### Task 5: LLM 传输层退避重试

**Files:**
- Modify: `platform/rsi/llm/providers.py`（AnthropicCompatHandler 增重试循环 + 异常分类）
- Modify: `platform/rsi/settings.py:572-603`（LlmSettings 增 `retry_max: int = 3`）
- Test: `platform/tests/unit/test_llm_providers.py`（追加；该文件已存在）

**Interfaces:**
- Consumes: DD-02 §5.1 伪码（`sleep(min(2**attempt, 30) + uniform(0, 1))`；RetryableHTTP=429/5xx/网络/超时）
- Produces: `RetryableProviderError`、`ProviderHTTPError`（providers.py 导出，Task 6 备链消费）；`llm.retry_max` 设置键（Task 8 配置文档登记）

- [ ] **Step 1: 写失败测试**（追加到 test_llm_providers.py）

```python
class _FlakyTransport:
    """前 N 次 500，之后 200——驱动退避重试路径。"""

    def __init__(self, fails: int, body: dict):
        self._fails = fails
        self._body = body
        self.calls = 0
        self.sleeps: list[float] = []

    def __call__(self, handler: AnthropicCompatHandler, *a, **k):
        import httpx

        handler_calls = {"n": 0}

        def poster(url, **kw):
            self.calls += 1
            if self.calls <= self._fails:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=self._body)

        return poster


def test_handler_retries_on_5xx(monkeypatch):
    import httpx

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 3, "output_tokens": 4}}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        "rsi.llm.providers.time.sleep", lambda s: sleeps.append(s)
    )
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    assert h("judge", "root_cause", {"prompt": "p"}) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # 两次失败各退避一次
    assert all(0 < s <= 31 for s in sleeps)  # min(2^n,30)+抖动


def test_handler_raises_after_retry_exhaustion(monkeypatch):
    import httpx

    def fake_post(url, **kw):
        return httpx.Response(500, text="down")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("rsi.llm.providers.time.sleep", lambda s: None)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    import pytest

    from rsi.llm.providers import RetryableProviderError

    with pytest.raises(RetryableProviderError):
        h("judge", "root_cause", {"prompt": "p"})


def test_handler_no_retry_on_4xx(monkeypatch):
    import httpx

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    monkeypatch.setattr(httpx, "post", fake_post)
    h = AnthropicCompatHandler(api_key="k", retry_max=3)
    from rsi.llm.providers import ProviderHTTPError

    with pytest.raises(ProviderHTTPError):
        h("judge", "root_cause", {"prompt": "p"})
    assert calls["n"] == 1  # 4xx 不重试
```

（删除 `_FlakyTransport` 辅助类——上面三个测试用 monkeypatch 已自洽。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_llm_providers.py -v`
Expected: 新增 3 FAIL（`retry_max` 参数不存在 / 异常类未定义）

- [ ] **Step 3: 最小实现**（providers.py）

```python
import random
import time

from rsi.core.errors import LLMError


class RetryableProviderError(LLMError):
    """传输层重试耗尽（429/5xx/网络/超时）."""


class ProviderHTTPError(LLMError):
    """非重试类 provider HTTP 错误（4xx 等）."""
```

`__init__` 增 `retry_max: int = 3`；`__call__` 的请求段改为：

```python
        last_retryable: Exception | None = None
        for attempt in range(self.retry_max):
            try:
                resp = httpx.post(
                    f"{self.base_url}/v1/messages",
                    json=body,
                    headers=headers,
                    timeout=self.timeout_s,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_retryable = exc
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            if resp.status_code == 200:
                break
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_retryable = ProviderHTTPError(
                    f"llm provider http {resp.status_code}: {resp.text[:300]}"
                )
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            raise ProviderHTTPError(
                f"llm provider http {resp.status_code}: {resp.text[:300]}"
            )
        else:
            raise RetryableProviderError(
                f"llm provider unreachable after {self.retry_max} attempts: "
                f"{last_retryable}"
            )
```

（`rsi.core.errors` 若无 `LLMError` 基类，则继承 `RuntimeError`；`import time/random` 置文件头。200 分支后的解析段保持现状。）

settings.py `LlmSettings` 增 `retry_max: int = 3`；bootstrap.py:242 构造处传 `retry_max=settings.llm.retry_max`。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd platform && uv run pytest tests/unit/test_llm_providers.py tests/unit/test_llm_client.py tests/unit/test_settings.py -v`
Expected: 全 PASS
Run: `cd platform && uv run pytest -q`
Expected: ≥1464 passed + 12 skipped

- [ ] **Step 5: Commit**

```bash
git add platform/rsi/llm/providers.py platform/rsi/settings.py platform/rsi/bootstrap.py platform/tests/unit/test_llm_providers.py
git commit -m "feat(llm): B1-5 provider 传输层退避重试（DD-02 §5.1，429/5xx/网络）"
```

---

### Task 6: backup_provider 故障转移链

**Files:**
- Modify: `platform/rsi/settings.py`（LlmProviderSettings 增 `backup_provider: str | None = None`）
- Modify: `platform/rsi/llm/providers.py`（新增 `ProviderChain`）
- Modify: `platform/rsi/bootstrap.py:240-251`（按 backup 配置装配链）
- Test: `platform/tests/unit/test_llm_provider_chain.py`（新建）

**Interfaces:**
- Consumes: `RetryableProviderError`/`ProviderHTTPError`（Task 5 产出）；`settings.llm.providers: dict[str, LlmProviderSettings]`
- Produces: `ProviderChain(primary, backup)`——callable Handler 协议（`(component, prompt_name, vars, *, temperature) -> str | tuple[str, dict]`），主连败（RetryableProviderError）切备；Task 7 的 usage 元组协议在链上透传

- [ ] **Step 1: 写失败测试**

```python
"""B1-6：backup_provider 故障转移（DD-02 §5.1 ProviderDown → switch）."""
from __future__ import annotations

import pytest

from rsi.llm.providers import ProviderChain, RetryableProviderError


class _Handler:
    def __init__(self, result, raise_retryable=False):
        self._result = result
        self._raise = raise_retryable
        self.calls = 0

    def __call__(self, component, prompt_name, vars, *, temperature=0.0):
        self.calls += 1
        if self._raise:
            raise RetryableProviderError("primary down")
        return self._result


def test_chain_falls_back_to_backup():
    primary = _Handler(None, raise_retryable=True)
    backup = _Handler("from-backup")
    chain = ProviderChain(primary, backup)
    assert chain("judge", "p", {"prompt": "x"}) == "from-backup"
    assert primary.calls == 1 and backup.calls == 1


def test_chain_primary_success_never_touches_backup():
    primary = _Handler("from-primary")
    backup = _Handler("unused")
    chain = ProviderChain(primary, backup)
    assert chain("judge", "p", {"prompt": "x"}) == "from-primary"
    assert backup.calls == 0


def test_chain_raises_when_both_down():
    chain = ProviderChain(
        _Handler(None, raise_retryable=True),
        _Handler(None, raise_retryable=True),
    )
    with pytest.raises(RetryableProviderError):
        chain("judge", "p", {"prompt": "x"})


def test_chain_passes_through_non_retryable():
    from rsi.llm.providers import ProviderHTTPError

    def bad(component, prompt_name, vars, *, temperature=0.0):
        raise ProviderHTTPError("401")

    chain = ProviderChain(bad, _Handler("unused"))
    with pytest.raises(ProviderHTTPError):
        chain("judge", "p", {"prompt": "x"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_llm_provider_chain.py -v`
Expected: 4 FAIL（ProviderChain 未定义）

- [ ] **Step 3: 最小实现**（providers.py 追加）

```python
class ProviderChain:
    """主备 handler 链：主 RetryableProviderError → 切备；其余异常直抛（DD-02 §5.1）."""

    def __init__(self, primary, backup=None):
        self._primary = primary
        self._backup = backup

    def __call__(self, component, prompt_name, vars, *, temperature=0.0):
        try:
            return self._primary(
                component, prompt_name, vars, temperature=temperature
            )
        except RetryableProviderError:
            if self._backup is None:
                raise
            return self._backup(
                component, prompt_name, vars, temperature=temperature
            )
```

settings.py `LlmProviderSettings` 增字段：

```python
    backup_provider: Optional[str] = None
```

bootstrap.py 装配段（:240-251 区域）扩展：

```python
    if settings.llm.cassette_mode == "off" and _anth is not None and _anth.api_key:
        handler = AnthropicCompatHandler(
            api_key=_anth.api_key,
            base_url=_anth.base_url,
            model=(judge_cfg.model if judge_cfg and judge_cfg.model else "glm-5.1"),
            retry_max=settings.llm.retry_max,
        )
        _bk_name = _anth.backup_provider
        _bk = settings.llm.providers.get(_bk_name) if _bk_name else None
        if _bk is not None and _bk.api_key:
            _bk_model = (settings.llm.defaults.get("judge").model
                         if settings.llm.defaults.get("judge") else "")
            handler = ProviderChain(
                handler,
                AnthropicCompatHandler(
                    api_key=_bk.api_key,
                    base_url=_bk.base_url,
                    model=_bk_model or "glm-5.1",
                    retry_max=settings.llm.retry_max,
                ),
            )
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd platform && uv run pytest tests/unit/test_llm_provider_chain.py tests/unit/test_bootstrap.py -v`
Expected: 全 PASS
Run: `cd platform && uv run pytest -q`
Expected: ≥1468 passed + 12 skipped

- [ ] **Step 5: Commit**

```bash
git add platform/rsi/llm/providers.py platform/rsi/settings.py platform/rsi/bootstrap.py platform/tests/unit/test_llm_provider_chain.py
git commit -m "feat(llm): B1-6 backup_provider 故障转移链（DD-02 ProviderDown switch）"
```

---

### Task 7: provider 真实 usage 回填

**Files:**
- Modify: `platform/rsi/llm/providers.py`（AnthropicCompatHandler.__call__ 返回 `(text, usage_dict)`）
- Modify: `platform/rsi/llm/client.py:163-257`（complete() 解包元组、LLMResponse.usage 真实值、估算路径 estimated=True）
- Test: `platform/tests/unit/test_llm_client.py`（追加）

**Interfaces:**
- Consumes: Handler 返回 `str | tuple[str, dict]`（新协议，向后兼容纯 str——测试桩/mock 既有形态不破坏）
- Produces: `LLMResponse.usage.estimated ∈ {True, False}`；llm_calls 表 input_tokens/output_tokens 为 provider 真实值（估算路径 len//4 标 estimated）

- [ ] **Step 1: 写失败测试**（追加到 test_llm_client.py）

```python
@pytest.mark.asyncio
async def test_complete_uses_real_usage_from_handler():
    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "plain text", {"input_tokens": 11, "output_tokens": 22}

    client = LLMClient(handler=handler)
    resp = await client.complete("judge", "root_cause", {"q": "x"})
    assert resp.usage.input_tokens == 11
    assert resp.usage.output_tokens == 22
    assert resp.usage.estimated is False


@pytest.mark.asyncio
async def test_complete_estimates_usage_for_str_handler():
    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "plain text"

    client = LLMClient(handler=handler)
    resp = await client.complete("judge", "root_cause", {"q": "x"})
    assert resp.usage.estimated is True
    assert resp.usage.input_tokens >= 1


@pytest.mark.asyncio
async def test_cassette_record_stores_real_usage(tmp_path):
    from rsi.llm.cassette import CassetteStore

    def handler(component, prompt_name, vars, *, temperature=0.0):
        return "ok", {"input_tokens": 7, "output_tokens": 9}

    cs = CassetteStore(root=tmp_path, mode="record")
    client = LLMClient(handler=handler, cassette=cs)
    await client.complete("judge", "root_cause", {"q": "y"})
    # record 落盘的 input/output_tokens 为真实值（非 len//4）
    files = list((tmp_path).rglob("*.json"))
    assert files, "cassette record should persist"
    import json as _json

    rec = _json.loads(files[0].read_text(encoding="utf-8"))
    entry = rec[-1] if isinstance(rec, list) else rec
    assert entry.get("input_tokens") == 7
    assert entry.get("output_tokens") == 9
```

（CassetteStore 构造参数以 `rsi/llm/cassette.py` 实际签名为准——先读该文件 `__init__` 与 `append` 载荷结构，按实际调整 tmp CassetteStore 的建法与断言路径。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd platform && uv run pytest tests/unit/test_llm_client.py -v`
Expected: 新增 3 FAIL（元组被当 str 用/无 estimated 逻辑）

- [ ] **Step 3: 最小实现**

providers.py `__call__` 末尾改返回元组：

```python
        usage = data.get("usage") or {}
        return text, {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }
```

（ProviderChain 无需改——直接透传返回值。）

client.py `complete()` 中 handler 调用段（:189-191）与响应构造段（:220-226）：

```python
                result = await asyncio.to_thread(
                    self._handler, component, prompt_name, call_vars, **kwargs
                )
                if isinstance(result, tuple):
                    raw, usage_info = result[0], result[1]
                else:
                    raw, usage_info = result, None
```

```python
        if usage_info:
            usage = LLMUsage(
                input_tokens=int(usage_info.get("input_tokens", 0) or 0),
                output_tokens=int(usage_info.get("output_tokens", 0) or 0),
                estimated=False,
            )
        else:
            usage = LLMUsage(
                input_tokens=max(len(prompt_text or "") // 4, 1),
                output_tokens=max(len(raw or "") // 4, 1),
                estimated=True,
            )
        resp = LLMResponse(
            content=raw or "",
            parsed=parsed,
            usage=usage,
            model=self.model,
            prompt_version=prompt_version,
        )
```

cassette record 段（:230-244）改用 `usage.input_tokens`/`usage.output_tokens`；`_account` 调用（:247-256）改传真实值（`usage.input_tokens, usage.output_tokens`）。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd platform && uv run pytest tests/unit/test_llm_client.py tests/unit/test_llm_providers.py tests/unit/test_llm_provider_chain.py -v`
Expected: 全 PASS
Run: `cd platform && uv run pytest -q`
Expected: ≥1471 passed + 12 skipped

- [ ] **Step 5: Commit**

```bash
git add platform/rsi/llm/providers.py platform/rsi/llm/client.py platform/tests/unit/test_llm_client.py
git commit -m "feat(llm): B1-7 provider 真实 usage 回填（llm_calls 不再 len//4 估算）"
```

---

### Task 8: 配置文档滚动 + B1 批次报告

**Files:**
- Modify: `docs/_rsi_playground_configuration.md`（llm 段增 `llm.retry_max`、`llm.providers.<name>.backup_provider`；修订记录滚动版本）
- Create: `platform/.fB1-report.md`（批次报告，合并前按惯例清理/不提交）

**Interfaces:**
- Consumes: Task 5/6 产出的设置键
- Produces: 配置文档 vNext（B2 起引用）

- [ ] **Step 1: 配置文档补键**

`docs/_rsi_playground_configuration.md` llm 表（68-77 行区）追加两行（格式仿该表既有行）：

```markdown
| llm.retry_max | int | 3 | provider 传输层退避重试次数（429/5xx/网络；DD-02 §5.1） |
| llm.providers.<name>.backup_provider | str \| null | null | 备 provider 名（须为 providers 内已配置键；主连败切换） |
```

修订记录头部滚动一条 B1 说明（版本号按文档现行递增惯例）。

- [ ] **Step 2: 写批次报告**（`platform/.fB1-report.md`）

按项目 `.f*-report.md` 惯例：每 task 一段（改动文件、测试数、commit hash）；结尾列"登记不阻塞"（如：外部驱动 session.py 的 evalset_id 传参待 B6 更新）。

- [ ] **Step 3: 终态回归**

Run: `cd platform && uv run pytest -q`
Expected: ≥1471 passed + 12 skipped，0 failed

- [ ] **Step 4: Commit（仅文档）**

```bash
git add docs/_rsi_playground_configuration.md
git commit -m "docs(config): B1-8 登记 llm.retry_max 与 backup_provider 键"
```

（`.fB1-report.md` 是工作产物，不提交。）

---

## 计划自审记录

- **Spec 覆盖**：B1 四项 ↔ Task 1-2（store 持久化+DB 行）、Task 3（FK）、Task 4（doctor）、Task 5-7（LLM 健壮化三件）、Task 8（文档滚动）——全覆盖；"runs 落库 lab_id/eval_task_id 核验"并入 Task 3 Step 4 的回归观察（closed_loop 集成测试既有断言覆盖 runs 行形状，若发现空值缺口在 Task 3 内一并补）。
- **占位符扫描**：无 TBD/TODO；三处"以实现/现有文件为准"的注记均为指向既有代码的对齐指令而非缺口。
- **类型一致性**：`RetryableProviderError/ProviderHTTPError/ProviderChain`（Task 5 定义、Task 6 消费）；`evalset_id: str = ""` 参数（iterate_once/_iterate_body 一致）；Handler 返回 `str | tuple[str, dict]`（Task 7 定义并在 chain 透传）——一致。
