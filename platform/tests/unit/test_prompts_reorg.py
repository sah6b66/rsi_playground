"""Tests: prompt 模板中文化与目录统一重组（rsi/llm/prompts/<component>/，DD-02 §2/§7）。

覆盖：
* 目录布局——模板全部位于 rsi/llm/prompts/<component>/，旧 rsi/judge/prompts/ 移除；
* DD-02 §7 初始 8 个模板齐备（judge 3 + attribution 1 + hypothesis 2 + critic 1 + sft 1），
  占位符集合与登记快照一致（前向契约：P1/P2 接入调用方时对齐）；
* 正文中文；JSON 输出键名保持英文（schema 校验硬约束）；
* 在用模板（root_cause/path_quality）以 llm_judge 实际 vars 渲染成功；
* 盲判约束（ADR-23 D-8）——模板不渲染任何评测上下文行；
* fallback 兜底 prompt 中文化；
* freeze 清单适配新布局（键前缀 prompts/ 指向 rsi/llm/prompts/judge）。
"""

from __future__ import annotations

import json
import shutil
import string
from pathlib import Path

from rsi.llm.prompts import PromptLibrary

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "rsi" / "llm" / "prompts"
OLD_JUDGE_PROMPTS = Path(__file__).resolve().parents[2] / "rsi" / "judge" / "prompts"

# DD-02 §7 初始 8 个模板：键 -> 占位符集合
EXPECTED_TEMPLATES: dict[str, set[str]] = {
    "judge/root_cause.v1": {
        "task_title", "expected", "actual", "confidence_of_sut", "match_hints",
    },
    "judge/path_quality.v1": {
        "steps_summary", "critical_steps", "checklist_items",
        "sut_actions", "task_complete", "actions",
    },
    "judge/checklist.v1": {"checklist_items", "sut_actions"},
    "attribution/classify.v1": {
        "cluster_samples", "sut_evidence", "failure_modes", "opt_history",
    },
    "hypothesis/diagnose.v1": {"cluster_summary", "evidence"},
    "hypothesis/propose.v1": {
        "problem_statement", "element_texts", "enabled_targets", "failed_hypotheses",
    },
    "critic/review.v1": {"changeset_summary", "det_results", "review_checklist"},
    "sft/draft.v1": {"defect_clusters", "opt_history"},
}

# 在用调用点（llm_judge）实际传入的 vars —— 翻译不得破坏占位符契约
CALLER_VARS: dict[str, dict[str, str]] = {
    "root_cause": {
        "task_title": "接口翻滚故障排查",
        "expected": "OSPF 邻居因 MTU 不匹配中断",
        "actual": "OSPF 邻居down，接口MTU配置错误",
        "confidence_of_sut": "0.8",
        "match_hints": "关注 MTU/邻居状态",
    },
    "path_quality": {
        "steps_summary": "step1: display interface; step2: display ospf peer",
        "critical_steps": "display interface",
        "checklist_items": "检查OSPF邻居状态",
        "sut_actions": "display interface",
        "task_complete": "True",
        "actions": "display interface",
    },
}


def _placeholders(body: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(body) if f}


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _load_real_library() -> PromptLibrary:
    lib = PromptLibrary()
    lib.load_root(PROMPTS_ROOT)
    return lib


# ---------------------------------------------------------------------------
# 目录布局与模板齐备性
# ---------------------------------------------------------------------------


class TestPromptLayout:
    def test_old_judge_prompts_dir_removed(self):
        assert not OLD_JUDGE_PROMPTS.exists(), (
            "旧目录 rsi/judge/prompts/ 应已迁移至 rsi/llm/prompts/judge/"
        )

    def test_all_dd02_templates_present(self):
        lib = _load_real_library()
        for key in EXPECTED_TEMPLATES:
            component, name_v = key.split("/", 1)
            name = name_v.rsplit(".v", 1)[0]
            assert lib.latest_version(component, name) >= 1, f"缺少模板 {key}"

    def test_orphan_consistency_template_gone(self):
        """consistency.v1 是无调用方的孤儿模板（一致性按 DD-08 D-8 是确定性规则）。"""
        lib = _load_real_library()
        assert lib.latest_version("judge", "consistency") == 0

    def test_templates_are_chinese(self):
        for key in EXPECTED_TEMPLATES:
            component, name_v = key.split("/", 1)
            name = name_v.rsplit(".v", 1)[0]
            lib = _load_real_library()
            body = lib._templates[f"{component}/{name_v}"]
            assert _has_cjk(body), f"{key} 正文应含中文"

    def test_placeholder_snapshots(self):
        lib = _load_real_library()
        for key, expected_vars in EXPECTED_TEMPLATES.items():
            body = lib._templates[key]
            assert _placeholders(body) == expected_vars, (
                f"{key} 占位符漂移: {_placeholders(body)} != {expected_vars}"
            )


# ---------------------------------------------------------------------------
# 在用模板渲染与盲判约束
# ---------------------------------------------------------------------------


class TestInUseTemplates:
    def test_render_with_caller_vars(self):
        lib = _load_real_library()
        for name, vars_ in CALLER_VARS.items():
            out = lib.render("judge", name, dict(vars_))
            assert _has_cjk(out)
            assert "display interface" in out or "OSPF" in out

    def test_json_keys_stay_english(self):
        lib = _load_real_library()
        out = lib.render("judge", "root_cause", dict(CALLER_VARS["root_cause"]))
        assert '"match"' in out and '"confidence"' in out

    def test_blind_judge_no_eval_context(self):
        """模板渲染结果不得出现评测上下文（臂别/批次信息）行。"""
        lib = _load_real_library()
        for name, vars_ in CALLER_VARS.items():
            out = lib.render("judge", name, dict(vars_))
            for leak in ("Evaluation context", "baseline", "candidate", "batch_purpose"):
                assert leak not in out, f"{name}: 模板泄漏 {leak!r}"


# ---------------------------------------------------------------------------
# fallback prompt 中文化
# ---------------------------------------------------------------------------


class TestFallbackChinese:
    def test_fallback_starts_with_chinese_component_line(self):
        lib = PromptLibrary()
        out = lib.render("judge", "score", {"k1": "v1"})
        assert out.startswith("组件: judge")
        assert "k1: v1" in out

    def test_fallback_empty_library(self):
        lib = PromptLibrary()
        assert "组件: critic" in lib.render("critic", "review", {"a": 1})


# ---------------------------------------------------------------------------
# freeze 清单适配新布局
# ---------------------------------------------------------------------------


class TestFreezeNewLayout:
    def test_repo_freeze_verify_ok(self):
        from rsi.judge.rules import freeze_check

        assert freeze_check.verify() is True

    def test_manifest_keys_follow_new_layout(self):
        from rsi.judge.rules import freeze_check

        manifest = json.loads(freeze_check.MANIFEST_PATH.read_text(encoding="utf-8"))
        prompt_keys = [k for k in manifest if k.startswith("prompts/")]
        rule_keys = [k for k in manifest if not k.startswith("prompts/")]
        assert rule_keys == ["rules/safety_rules.yaml"]
        assert len(prompt_keys) >= 3  # root_cause / path_quality / checklist
        assert all(not k.startswith("prompts/prompts/") for k in prompt_keys)

    def test_roundtrip_and_tamper_detection(self, tmp_path):
        import shutil

        from rsi.judge.rules import freeze_check

        rules_root = tmp_path / "judge"
        prompts_root = tmp_path / "prompts_judge"
        shutil.copytree(freeze_check.JUDGE_ROOT / "rules", rules_root / "rules")
        shutil.copytree(freeze_check.PROMPTS_ROOT, prompts_root)
        manifest_path = rules_root / "rules" / "freeze.manifest"
        freeze_check.write_manifest(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )
        assert freeze_check.verify(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prompt_key = next(k for k in manifest if k.startswith("prompts/"))
        target = prompts_root / prompt_key.removeprefix("prompts/")
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# tampered", encoding="utf-8"
        )
        assert not freeze_check.verify(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )

    def test_digest_tamper_fails(self, tmp_path):
        from rsi.judge.rules import freeze_check

        manifest = json.loads(
            freeze_check.MANIFEST_PATH.read_text(encoding="utf-8")
        )
        prompt_key = next(k for k in manifest if k.startswith("prompts/"))
        manifest[prompt_key] = "0" * 64
        bad = tmp_path / "freeze.manifest"
        bad.write_text(json.dumps(manifest), encoding="utf-8")
        assert not freeze_check.verify(bad)

    def test_unlisted_prompt_file_fails(self, tmp_path):
        """新增未列册的 .v2 模板文件必须 fail（R2-H2：防私换活跃模板）。"""
        import shutil

        from rsi.judge.rules import freeze_check

        rules_root = tmp_path / "judge"
        prompts_root = tmp_path / "prompts_judge"
        shutil.copytree(freeze_check.JUDGE_ROOT / "rules", rules_root / "rules")
        shutil.copytree(freeze_check.PROMPTS_ROOT, prompts_root)
        manifest_path = rules_root / "rules" / "freeze.manifest"
        freeze_check.write_manifest(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )
        assert freeze_check.verify(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )
        (prompts_root / "root_cause.v2.md").write_text("x", encoding="utf-8")
        assert not freeze_check.verify(
            manifest_path, rules_root=rules_root, prompts_root=prompts_root
        )
