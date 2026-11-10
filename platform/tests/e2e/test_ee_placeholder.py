"""EE placeholder cases (T2-9): E2E acceptance runs on compose/playwright.

The full offline platform stack is exercised by tests/integration; these
placeholders keep the 9 documented E2E acceptance cases visible and let
``pytest -m e2e`` run (all skips, no errors) until the compose/playwright
phase lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

# EE-007 已由 playwright golden-path 承接（T8）：platform/web/tests/e2e/
# golden-path.spec.ts（离线后端 + 真实浏览器；`cd platform/web && npx
# playwright test`）。spec 存在时本用例跳过；spec 被移除时回落为占位提醒。
_WEB_E2E_SPEC = (
    Path(__file__).resolve().parents[2] / "web" / "tests" / "e2e"
    / "golden-path.spec.ts"
)


def test_ee_001_p0_acceptance_sut_versions() -> None:
    """EE-001: OpenCode 示例 SUT 构建与双配置版本可区分轨迹。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_002_p0_acceptance_ten_task_one_shot() -> None:
    """EE-002: 10 任务数据集一键全链路。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_003_p0_acceptance_double_run_consistency() -> None:
    """EE-003: 同用例双跑一致性（ADR-23 双口径）。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_004_p0_acceptance_rollback_rerun() -> None:
    """EE-004: 历史版本一键回滚并以该版本重跑。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_005_p0_acceptance_llm_dashboard_compare() -> None:
    """EE-005: 双 LLM 配置各跑一轮 → dashboard 对比。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_006_p0_acceptance_ab_annotation_sft_report() -> None:
    """EE-006: 失败轨迹 A/B 标注 + SFT 评估报告模板生成。"""
    pytest.skip("E2E: compose/playwright phase")


@pytest.mark.skipif(
    _WEB_E2E_SPEC.is_file(),
    reason="由 web/tests/e2e/golden-path.spec.ts 承接（playwright，EE-007）",
)
def test_ee_007_frontend_smoke() -> None:
    """EE-007: 登录→批次详情（拓扑图/轨迹时间线/diff 渲染）。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_008_trace_completeness() -> None:
    """EE-008: 超长输出全量入库、ts 单调、日志按 run_id 检索、SUT 日志归档。"""
    pytest.skip("E2E: compose/playwright phase")


def test_ee_009_network_isolation() -> None:
    """EE-009: SUT 容器访问平台端口应失败；lab 网络互不可达。"""
    pytest.skip("E2E: compose/playwright phase")
