"""OpenAPI schema customization (DD-13 §4.5).

Customizes the generated ``/openapi.json``: ordered ``tags`` with Chinese
descriptions (one entry per router group), an explicit ``servers`` list and
a shared ``Error`` schema in ``components`` so the frontend client
generator (openapi-typescript) emits usable error types.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

# Ordered per ALL_ROUTERS in rsi.api.routers (auth first, ops last).
# Descriptions are Chinese per DD-13 §4.5 ①.
TAG_DESCRIPTIONS: list[dict[str, str]] = [
    {"name": "auth", "description": "认证与令牌：登录、刷新、登出"},
    {"name": "suts", "description": "SUT 管理：受测系统注册与查询"},
    {"name": "versions", "description": "配置版本：Git 存储版本查询与导出"},
    {"name": "hypotheses", "description": "假设管理：H1-H8 假设的增删改查"},
    {"name": "credentials", "description": "凭据管理：外部服务凭据的登记与轮换"},
    {"name": "users", "description": "用户管理：账号、角色与禁用状态"},
    {"name": "evalsets", "description": "评测集：导入、校验与噪声标定"},
    {"name": "profiles", "description": "资源档位：设备/预算档位配置"},
    {"name": "batches", "description": "批次：触发、状态、终止与覆盖率"},
    {"name": "instrumentation", "description": "插装：批次插装报告与工件"},
    {"name": "runs", "description": "运行：单 run 详情与轨迹"},
    {"name": "iterations", "description": "迭代引擎：启动、暂停、恢复与状态机"},
    {"name": "labs", "description": "实验室：临时 SUT 环境的创建与销毁"},
    {"name": "instances", "description": "实例：运行实例的观测与控制"},
    {"name": "judgments", "description": "评审：自动评审与人工门 gate2"},
    {"name": "experiments", "description": "实验：A/B 对照实验管理"},
    {"name": "traces", "description": "轨迹：run 轨迹事件查询"},
    {"name": "system", "description": "运维观测：健康、依赖、STOP 与诊断"},
    {"name": "dashboard", "description": "仪表盘：汇总统计与趋势"},
    {"name": "audit-logs", "description": "审计日志：@audited 操作留痕查询"},
    {"name": "streams", "description": "流式推送：SSE 事件流与 WS run 轨迹"},
]

# Aligned with rsi.api.errors.ErrorResponse (the global RsiError handlers
# emit this shape for every non-2xx application response).
_ERROR_SCHEMA: dict[str, Any] = {
    "title": "Error",
    "description": (
    "统一错误响应（rsi.api.errors 全局异常处理器产出）。"
    "code 取值：UNAUTHORIZED / FORBIDDEN / NOT_FOUND / VALIDATION / "
    "CONFLICT / BUDGET_EXCEEDED / SERVICE_UNAVAILABLE / LLM_INVALID / "
    "INTERNAL。"
    ),
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "机器可读错误码"},
        "message": {"type": "string", "description": "人类可读错误信息"},
        "detail": {
            "type": "object",
            "nullable": True,
            "description": "附加上下文（run_id/batch_id 等），可为空",
        },
    },
    "required": ["code", "message"],
}


def customize_openapi(app: FastAPI, api_prefix: str = "/api/v1") -> dict:
    """Build (and cache via ``app.openapi_schema``) the customized schema.

    Standard FastAPI recipe: assign ``app.openapi = lambda:
    customize_openapi(app)`` in the app factory.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        tags=TAG_DESCRIPTIONS,
    )
    schema["servers"] = [
        {
            "url": api_prefix,
            "description": "API 挂载前缀（相对路径，随部署域名生效）",
        },
        {
            "url": f"http://localhost:8000{api_prefix}",
            "description": "本地开发默认地址",
        },
    ]
    info = schema.setdefault("info", {})
    info["description"] = (
        "RSI Playground 平台 API。所有非 2xx 业务错误返回统一 Error 结构"
        "（components.schemas.Error）。"
    )
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["Error"] = _ERROR_SCHEMA
    app.openapi_schema = schema
    return schema
