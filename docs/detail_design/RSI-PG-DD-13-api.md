# RSI-PG-DD-13 api —— Platform API 层

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-13 |
| 版本 | v1.0 |
| 上游 | 设计 3.10、接口 §9；FR10、I-9；ADR-18/19 |
| 交付切片 | S9（api+web 最小版） |

## 1. 追溯与范围

**职责**：FastAPI 装配（19 组 Router + DI 容器 + SSE/WS + 错误处理器 + @audited 全量应用）。是**唯一**对外暴露面——全部日常操作经此层完成。

**不做什么**：不含业务逻辑（路由只做参数校验+调服务+包装响应）；不做前端渲染（web 独立 SPA，DD-14）；不直接操作 DB/文件（经服务层）。

## 2. 文件与类清单

```
rsi/api/__init__.py
rsi/api/app.py               # create_app() FastAPI 工厂
rsi/api/deps.py              # 依赖注入容器（get_db/get_current_user/get_services）
rsi/api/errors.py            # 全局异常处理器（RsiError→HTTP 映射）
rsi/api/streaming.py         # SSE + WS 端点
rsi/api/openapi.py           # OpenAPI schema 定制（tag 分组/描述/示例）
rsi/api/routers/__init__.py
rsi/api/routers/auth.py      # 9.1
rsi/api/routers/suts.py      # 9.2（含 files CRUD + versions:snapshot）
rsi/api/routers/versions.py  # 9.3
rsi/api/routers/evalsets.py  # 9.4
rsi/api/routers/profiles.py  # 9.5
rsi/api/routers/batches.py   # 9.6
rsi/api/routers/runs.py      # 9.7
rsi/api/routers/traces.py    # 9.8（streams + diff + query/stats）
rsi/api/routers/judgments.py # 9.9
rsi/api/routers/hypotheses.py# 9.10
rsi/api/routers/experiments.py # 9.11（含 compare + sft-report）
rsi/api/routers/iterations.py# 9.12（含四门端点）
rsi/api/routers/labs.py      # 9.13
rsi/api/routers/instances.py # 9.14
rsi/api/routers/instrumentation.py # 9.15
rsi/api/routers/approvals.py # 9.16（P2）
rsi/api/routers/dashboard.py # 9.17
rsi/api/routers/audit_logs.py# 9.18
rsi/api/routers/system.py    # 9.19（credentials/users/settings/stop/freeze/logs/health）
```

## 3. 数据结构

```python
# 响应模型（Pydantic，OpenAPI 自动生成前端客户端）
class Page(BaseModel, Generic[T]):
    items: list[T]; next_cursor: str | None; has_more: bool

class ErrorResponse(BaseModel):
    code: str; message: str; detail: dict | None = None

# x-pagination 响应头（游标分页）
# 格式：json {"next_cursor": "…", "has_more": true}

# DI 容器键（deps.py）
SERVICE_KEYS = ["gitstore", "host_service", "sandbox_service", "trace_collector",
                "trace_store", "judge_service", "batch_runner", "iteration_engine",
                "budget_controller", "event_bus", "auth_service", "credential_store",
                "settings_manager"]
```

## 4. 对外接口

### 4.1 create_app 工厂

```python
def create_app(settings: Settings, services: dict[str, Any]) -> FastAPI:
    """装配全部 Router + DI 容器 + 异常处理器 + 中间件（JWT 鉴权 + CORS + 审计）
    前缀 /api/v1； lifespan 中启动 EventBus 订阅 + STOP 轮询 + verify_freeze"""
```

### 4.2 依赖注入（deps.py）

```python
def get_db() -> Iterator[Session]: …               # SQLModel session（每请求）
def get_current_user(token=Depends(oauth2), db=Depends(get_db)) -> User: …
def require(*perms: Permission): …                  # RBAC 依赖工厂
def get_service(key: str) -> Callable: …             # 从 DI 容器取服务实例
```

### 4.3 全局异常处理器（errors.py）

```python
EXCEPTION_MAP = {
    AuthError:            (401, "UNAUTHORIZED"),
    PermissionError:      (403, "FORBIDDEN"),
    NotFoundError:        (404, "NOT_FOUND"),
    ValidationError:      (422, "VALIDATION"),
    ConflictError:        (409, "CONFLICT"),
    BudgetExceeded:       (429, "BUDGET_EXCEEDED"),
    TargetViolationError: (403, "FORBIDDEN"),       # 路径越界视同权限不足
    InvalidTransitionError: (409, "CONFLICT"),
    LLMOutputInvalid:     (502, "LLM_INVALID"),     # 上游 LLM 输出不可用
    RsiError:             (500, "INTERNAL"),        # 兜底
}
# 每个 handler 构造 ErrorResponse + 写 ERROR 日志（含 run_id/batch_id contextvars）
```

### 4.4 SSE / WS（streaming.py）

```python
@router.get("/streams/events")      # SSE
async def sse_events(user=Depends(get_current_user)):
    """EventSource 响应；订阅 EventBus → 逐条 yield f"data: {json.dumps(msg)}\n\n"
    事件类型：*.status_changed + system.alert（F4 v1.1 告警——前端 toast/徽章）
    客户端断开→ unsubscribe"""

@router.websocket("/streams/runs/{run_id}")   # WS
async def ws_trace(websocket: WebSocket, run_id: str):
    """短时效 token 鉴权（query 参数 → verify）
    首帧 {"type":"tail","events":[尾部 N=50 条]} → 后续 {"type":"append","event":…}
    30s 心跳 ping/pong"""

@router.get("/system/dependencies")  # H1-H8 v1.1 全外部依赖状态
@require("view:read")
async def get_dependencies(monitor=Depends(get_service("health_monitor"))) -> list[DependencyStatus]:
    return monitor.get_cached()  # HealthMonitor 30s 轮询缓存（DD-12 §4.7）
```
```

### 4.5 OpenAPI 定制（openapi.py）

```python
def customize_openapi(app: FastAPI):
    """① 按 19 组 Router 设 tags（中文描述）
    ② 全局 ErrorResponse schema 添加
    ③ examples 填充（关键端点附示例请求/响应 JSON）
    → openapi-typescript 生成前端客户端（web/src/api/）"""
```

## 5. 内部逻辑

### 5.1 路由模板（所有 Router 遵循）

```python
@router.post("/suts")
@require("sut:manage")
@audited("sut.create")
async def create_sut(body: SutCreate, db=Depends(get_db),
                     user=Depends(get_current_user)) -> SutResponse:
    sut = sut_service.create(db, body)
    return SutResponse.from_orm(sut)
```

**三层装饰**：`@require`（RBAC）→ `@audited`（审计）→ handler 本体。

### 5.2 游标分页约定

```
请求：GET /suts?cursor=<opaque>&limit=20
响应头：x-pagination: {"next_cursor": "eyJpZCI6…", "has_more": true}
cursor 编码：base64(json({last_id, sort_key}))
查询：WHERE id > last_id ORDER BY id LIMIT limit + 1 → has_more = (返回条数 > limit)
```

### 5.3 人工门端点（iterations.py）

```python
@router.post("/iterations/{id}/confirm-direction")
@require("batch:trigger")
@audited("gate.confirm_direction")
async def gate_direction(id: str, body: DirectionConfirm,
                         engine=Depends(get_service("iteration_engine"))):
    await engine.gate_confirm_direction(id, body.selected)
    return Response(status_code=204)
# 服务端校验：iteration.state == "PAUSED" and current_step == "awaiting_direction"
# 否则 → 409 InvalidTransitionError
# 其余三门同构（hypothesis/review/decision）
```

### 5.4 system 组特殊端点

```python
@router.get("/system/settings")
@require("credential:manage")  # admin only
async def get_settings(mgr=Depends(get_service("settings_manager"))) -> list[SettingItem]:
    return mgr.list_all()  # [{path, value, source, mutable, 需求号}]

@router.put("/system/settings/{path:path}")
@require("credential:manage")
@audited("system.settings_update")
async def update_setting(path: str, body: SettingUpdate, mgr=…):
    mgr.update(path, body.value)  # 动态项：写 yaml+热生效；静态项→409
```

## 6. 依赖矩阵

- imports：fastapi、uvicorn、pydantic、rsi/core、rsi/security、rsi/settings；各业务模块服务（仅 import 接口类）。
- 注入：create_app←services dict（由 S9 装配脚本组装）。
- DB：不直接操作（经 get_db 传给服务层）。
- 被依赖：web（DD-14 经 /openapi.json 生成客户端）；外部调用方（curl/CI）。

## 7. 测试映射与实施注记

**测试**：IN-008（RBAC 矩阵逐格+写操作进 audit_log）、IN-009（SSE/WS 推送）、IN-020（人工门四端点+非法状态 409）、IN-021（运维观测端点集）、EE-007（前端冒烟）；openapi schema 快照测试（CI 中对比 schema diff）。

**实施注记**：① 每个路由文件仅做参数校验+调服务+包装响应——禁止在路由层写业务逻辑/直接 SQL；② @audited 装饰器在 @require 之后（先鉴权再审计——鉴权失败不审计）；③ SSE 用 `StreamingResponse(media_type="text/event-server-sent-events")`；④ WS 鉴权 token 短时效（5 分钟过期，iterations 启动时生成）；⑤ openapi-typescript 命令：`npx openapi-typescript http://localhost:8000/api/v1/openapi.json -o web/src/api/schema.d.ts`；⑥ `create_app` 可选 `docs_url=None`（生产环境关闭 Swagger UI）；⑦ lifespan 中 `verify_freeze()` 启动失败→拒绝启动（FastAPI lifespan exception）。
