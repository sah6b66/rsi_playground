"""SIM placeholder cases (T2-9): real simulation backends land at S11+.

Each documented SIM case gets an explicit marker + skip so
``pytest -m sim`` runs (all skips, no errors) and the deferred status is
visible in CI output instead of silently absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.sim


def test_sim_001_ensp_pro() -> None:
    """SIM-001: ENSP Pro 模板构建→SSH 初始配置→故障注入→就绪探测。"""
    pytest.skip("S11: real backend required")


def test_sim_002_pnetlab() -> None:
    """SIM-002: PNETLab 同链路 + 快照降级等价性（reset+重放 vs 快照恢复）。"""
    pytest.skip("S11: real backend required")


def test_sim_003_reproducibility() -> None:
    """SIM-003: 真实后端同用例双跑（ADR-23 双口径一致性）。"""
    pytest.skip("S11: real backend required")
