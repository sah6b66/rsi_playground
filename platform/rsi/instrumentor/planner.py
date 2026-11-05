"""Planner: choose instrumentation method per scanned object (DD-06 §4)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from rsi.instrumentor.scanner import ScanReport


class PlanItem(BaseModel):
    target: str
    method: str
    file: str
    snippet_spec: dict[str, Any] | None = None
    rollback: str = "overlay"


class InstrumentPlan(BaseModel):
    items: list[PlanItem] = []


class Planner:
    @staticmethod
    def plan(scan: ScanReport, policy: dict | None = None) -> InstrumentPlan:
        policy = policy or {}
        items: list[PlanItem] = []

        # tool -> hook (preferred) or mcp_wrap per carrier support
        for srv in scan.mcp_servers:
            items.append(
                PlanItem(
                    target="tool",
                    method="mcp_wrap",
                    file="mcp.json",
                    snippet_spec={"server": srv["name"]},
                )
            )
        if scan.carrier_hooks_supported and scan.mcp_servers is not None:
            items.append(
                PlanItem(target="tool", method="hook", file="plugins/rsi-trace-hook.ts")
            )

        # skill/script -> source markers (P0: not instrumentable, marker only)
        for skill in scan.skills:
            items.append(
                PlanItem(
                    target="skill",
                    method="source",
                    file=skill["path"],
                    rollback="marker",
                )
            )
        for script in scan.scripts:
            items.append(
                PlanItem(
                    target="script",
                    method="source",
                    file=script["path"],
                    rollback="marker",
                )
            )
        return InstrumentPlan(items=items)
