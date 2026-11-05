"""Instrumentor facade: five-stage pipeline (DD-06 §4).

scan -> plan -> overlay build -> injector.apply -> verifier.verify.
Passed: return (overlay_dir, report). Failed: cleanup overlay and degrade to
(config_dir, report annotated with a warning).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from rsi.carrier.overlay import OverlayBuilder
from rsi.instrumentor.injector import Injector
from rsi.instrumentor.planner import Planner
from rsi.instrumentor.report import CoverageReport, NotInstrumentable, ReportGenerator
from rsi.instrumentor.scanner import Scanner
from rsi.instrumentor.verifier import Verifier
from rsi.log import get_logger

logger = get_logger(__name__)


class Instrumentor:
    def __init__(
        self,
        scanner: Scanner | None = None,
        planner: Planner | None = None,
        injector: Injector | None = None,
        verifier: Verifier | None = None,
        report: ReportGenerator | None = None,
    ) -> None:
        self.scanner = scanner or Scanner()
        self.planner = planner or Planner()
        self.injector = injector or Injector()
        self.verifier = verifier or Verifier()
        self.report = report or ReportGenerator()

    @staticmethod
    def instrument(
        config_dir: Path,
        carrier: str,
        smoke_task_result_fn: Callable[[Path], "object"],
    ) -> tuple[Path, CoverageReport]:
        config_dir = Path(config_dir)
        logger.debug("instrument.entry", config_dir=str(config_dir), carrier=carrier)

        scan = Scanner.scan(config_dir, carrier)
        plan = Planner.plan(scan, policy={})
        overlay_dir = OverlayBuilder.build(config_dir, plan)
        Injector.apply(overlay_dir, plan)

        base_result = smoke_task_result_fn(config_dir)
        overlay_result = smoke_task_result_fn(overlay_dir)
        overlay_events = Instrumentor._read_events(overlay_dir)
        verify = Verifier.verify(base_result, overlay_result, overlay_events)
        logger.debug("instrument.verify", passed=verify.passed)

        report = ReportGenerator.generate(plan, scan, verify)
        if verify.passed:
            logger.debug("instrument.exit", result="overlay", overlay_dir=str(overlay_dir))
            return overlay_dir, report

        logger.debug("instrument.degrade", reason="verify_failed")
        OverlayBuilder.cleanup(overlay_dir)
        report.not_instrumentable.append(
            NotInstrumentable(
                aspect="tool",
                reason=f"WARNING: smoke verify failed, overlay removed, degraded to original config_dir. {verify.detail}",
            )
        )
        return config_dir, report

    @staticmethod
    def _read_events(overlay_dir: Path) -> list[dict]:
        sink = overlay_dir / "trace" / "events.ndjson"
        if not sink.is_file():
            return []
        events: list[dict] = []
        for line in sink.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
