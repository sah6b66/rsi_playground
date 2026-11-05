"""Unit tests: PromptLibrary + Accountant + LLMClient wiring (DD-02 §4)."""

from __future__ import annotations

import pytest

from rsi.llm.accounting import Accountant
from rsi.llm.client import LLMClient
from rsi.llm.prompts import PromptLibrary


class TestPromptLibrary:
    def test_render_latest_version(self):
        lib = PromptLibrary(
            {
                "attribution/cluster.v1": "old {input}",
                "attribution/cluster.v3": "cluster the following: {input} (mode={mode})",
                "attribution/cluster.v2": "mid {input}",
            }
        )
        out = lib.render("attribution", "cluster", {"input": "runs", "mode": "rule"})
        assert out == "cluster the following: runs (mode=rule)"

    def test_latest_version(self):
        lib = PromptLibrary({"a/b.v1": "x", "a/b.v7": "y", "a/b.v10": "z", "other/c.v2": "w"})
        assert lib.latest_version("a", "b") == 10
        assert lib.latest_version("a", "missing") == 0

    def test_fallback_when_no_template(self):
        lib = PromptLibrary({"unrelated/x.v1": "t"})
        out = lib.render("judge", "score", {"k1": "v1", "k2": 2})
        assert out.startswith("Component: judge, Task: score")
        assert "k1: v1" in out and "k2: 2" in out

    def test_empty_library_renders_fallback(self):
        lib = PromptLibrary()
        assert "Component: critic" in lib.render("critic", "review", {"a": 1})


class TestAccountant:
    def test_record_and_sum(self):
        acc = Accountant()
        acc.record("attribution", "glm-4.7", "v1", 100, 50, 1200, cost_usd=0.01, run_id="r1")
        acc.record("judge", "glm-4.7", "v2", 200, 80, 800, cost_usd=0.02, run_id="r2", batch_id="b1")
        total = acc.sum_usage()
        assert total["calls"] == 2
        assert total["input_tokens"] == 300
        assert total["output_tokens"] == 130
        assert total["latency_ms"] == 2000
        assert total["cost_usd"] == pytest.approx(0.03)

    def test_sum_usage_filtered_by_scope(self):
        acc = Accountant()
        acc.record("attribution", "m", "v1", 100, 10, 100, run_id="r1", batch_id="b1")
        acc.record("judge", "m", "v1", 50, 5, 50, run_id="r2", batch_id="b1")
        acc.record("judge", "m", "v1", 7, 3, 30, run_id="r3", batch_id="b2")
        by_batch = acc.sum_usage("b1")
        assert by_batch["calls"] == 2
        assert by_batch["input_tokens"] == 150
        by_run = acc.sum_usage("r3")
        assert by_run["calls"] == 1 and by_run["input_tokens"] == 7

    def test_records_snapshot(self):
        acc = Accountant()
        acc.record("judge", "m", "v3", 1, 2, 3)
        recs = acc.records()
        assert len(recs) == 1
        assert recs[0]["component"] == "judge"
        assert recs[0]["cost_usd"] is None
        # snapshot is a copy
        recs.clear()
        assert len(acc.records()) == 1


class TestLLMClientWiring:
    @pytest.mark.asyncio
    async def test_complete_uses_prompt_library_and_records_accounting(self):
        lib = PromptLibrary({"judge/score.v2": "score this run: {run_id}"})
        acc = Accountant()
        seen: dict = {}

        def handler(component, prompt_name, vars):
            seen.update(component=component, prompt=vars.get("prompt"))
            return '{"score": 0.9}'

        client = LLMClient(handler, prompt_library=lib, accountant=acc)
        resp = await client.complete("judge", "score", {"run_id": "r1"}, run_id="r1")
        assert resp.prompt_version == "v2"
        assert seen["prompt"] == "score this run: r1"
        usage = acc.sum_usage("r1")
        assert usage["calls"] == 1
        assert usage["input_tokens"] >= 1 and usage["output_tokens"] >= 1

    @pytest.mark.asyncio
    async def test_complete_without_library_unchanged(self):
        client = LLMClient(lambda c, p, v: "plain text")
        resp = await client.complete("judge", "score", {"a": "b"})
        assert resp.content == "plain text"
        assert resp.prompt_version == "v1"
