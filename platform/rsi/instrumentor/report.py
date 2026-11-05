"""CoverageReport generation (I-8 contract)."""

from __future__ import annotations

from pydantic import BaseModel

from rsi.instrumentor.planner import InstrumentPlan
from rsi.instrumentor.scanner import ScanReport
from rsi.instrumentor.verifier import VerifyResult


class CoveredItem(BaseModel):
    aspect: str
    method: str
    files: list[str]


class NotInstrumentable(BaseModel):
    aspect: str
    reason: str


class CoverageReport(BaseModel):
    covered: list[CoveredItem] = []
    not_instrumentable: list[NotInstrumentable] = []


class ReportGenerator:
    def generate(
        plan: InstrumentPlan, scan: ScanReport, verify: VerifyResult
    ) -> CoverageReport:
        covered: list[CoveredItem] = []
        not_instrumentable: list[NotInstrumentable] = []

        marker_files = {i.file for i in plan.items if i.method == "source"}
        hook_files = {i.file for i in plan.items if i.method == "hook"}
        wrap_servers = sorted(
            (i.snippet_spec or {}).get("server", "")
            for i in plan.items
            if i.method == "mcp_wrap"
        )

        if wrap_servers:
            covered.append(
                CoveredItem(
                    aspect="tool",
                    method="mcp_wrap",
                    files=["mcp.json"] + [f"mcp.json#{s}" for s in wrap_servers],
                )
            )
        if hook_files:
            covered.append(CoveredItem(aspect="tool", method="hook", files=sorted(hook_files)))

        # llm channel is intentionally never instrumented (I-8)
        not_instrumentable.append(
            NotInstrumentable(aspect="llm", reason="LLM channel excluded by design (I-8)")
        )

        if marker_files:
            not_instrumentable.append(
                NotInstrumentable(
                    aspect="skill",
                    reason="P0: source-level markers only, no event capture (P1)",
                )
            )
            not_instrumentable.append(
                NotInstrumentable(
                    aspect="script",
                    reason="P0: source-level markers only, no event capture (P1)",
                )
            )
        if scan.knowledge is not None:
            not_instrumentable.append(
                NotInstrumentable(aspect="knowledge", reason="kb_proxy deferred to P1")
            )

        if not verify.passed:
            not_instrumentable.append(
                NotInstrumentable(aspect="tool", reason=f"smoke verify failed: {verify.detail}")
            )

        return CoverageReport(covered=covered, not_instrumentable=not_instrumentable)
