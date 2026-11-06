"""rsi.api.routers: endpoint modules (DD-13 §2)."""

from rsi.api.routers import (
    audit_logs,
    auth,
    batches,
    credentials,
    dashboard,
    evalsets,
    experiments,
    hypotheses,
    instances,
    instrumentation,
    iterations,
    judgments,
    labs,
    profiles,
    runs,
    suts,
    system,
    traces,
    users,
    versions,
)
from rsi.api import streaming

ALL_ROUTERS = [
    auth.router,
    suts.router,
    versions.router,
    hypotheses.router,
    credentials.router,
    users.router,
    evalsets.router,
    profiles.router,
    batches.router,
    runs.router,
    # extended (Task 4)
    iterations.router,
    labs.router,
    instances.router,
    judgments.runs_router,
    judgments.router,
    experiments.router,
    traces.router,
    # system + ops (Task 5)
    system.router,
    dashboard.router,
    audit_logs.router,
    instrumentation.router,
    streaming.router,
]

__all__ = ["ALL_ROUTERS", "audit_logs", "auth", "batches", "credentials",
           "dashboard", "evalsets", "experiments", "hypotheses",
           "instances", "instrumentation", "iterations", "judgments",
           "labs", "profiles", "runs", "suts", "streaming", "system",
           "traces", "users", "versions"]
