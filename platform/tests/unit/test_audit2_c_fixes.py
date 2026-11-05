"""TU for audit T1-c C-group fixes (C-1..C-7): tracing usage/protocol,
judge_prompt_hash, CassetteStore, frozen rule files, accounting persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session, select

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.llm.accounting import Accountant
from rsi.llm.cassette import CassetteMissError, CassetteStore
from rsi.llm.client import LLMClient
from rsi.llm.prompts import PromptLibrary
from rsi.tracing.parsers import OpenCodeWiretapParser


def _ev(seq: int, etype, data, run_id="run-c") -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut",
        seq=seq,
        ts="2026-01-01T00:00:00.000+00:00",
        event_type=etype,
        data=data,
    )


# ---------------------------------------------------------------------------
# C-2: http frame protocol probing (DD-07 §5.1)
# ---------------------------------------------------------------------------


class TestProtocolProbing:
    def _parse(self, body) -> object:
        parser = OpenCodeWiretapParser()
        frame = json.dumps({"type": "http", "kind": "primary", "at": "t", "body": body})
        return parser.parse_line(frame, "run-1", "t1", "lab1", "sut1")

    @pytest.mark.parametrize(
        "body,protocol",
        [
            ({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "chat_completions"),
            ({"model": "m", "input": "hi"}, "responses"),
        ],
    )
    def test_dict_body_probes_protocol(self, body, protocol):
        ev = self._parse(body)
        assert ev is not None
        assert ev.data["protocol"] == protocol

    def test_json_string_body_probes_protocol(self):
        ev = self._parse(json.dumps({"input": "hi"}))
        assert ev is not None
        assert ev.data["protocol"] == "responses"

    @pytest.mark.parametrize("body", [{}, {"model": "m"}, "not json", None, 42])
    def test_unfeatured_body_leaves_protocol_unset(self, body):
        ev = self._parse(body)
        assert ev is not None
        assert "protocol" not in ev.data


# ---------------------------------------------------------------------------
# C-3: real usage preserved / estimation only fills the gap
# ---------------------------------------------------------------------------


class TestRealUsagePreserved:
    def test_frame_usage_preserved_verbatim(self):
        parser = OpenCodeWiretapParser()
        frame = json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "t",
                "system": [{"text": "sys"}],
                "messages": [{"role": "user", "content": "hello"}],
                "usage": {"input_tokens": 4321, "output_tokens": 77, "cost_usd": 0.02},
            }
        )
        ev = parser.parse_line(frame, "run-1", "t1", "lab1", "sut1")
        assert ev is not None
        usage = ev.data["usage"]
        assert usage["input_tokens"] == 4321
        assert usage["output_tokens"] == 77
        assert usage["cost_usd"] == 0.02
        assert usage["estimated"] is False

    def test_frame_without_usage_gets_estimate(self):
        parser = OpenCodeWiretapParser()
        frame = json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "t",
                "system": [{"text": "sys"}],
                "messages": [],
            }
        )
        ev = parser.parse_line(frame, "run-1", "t1", "lab1", "sut1")
        assert ev is not None
        assert ev.data["usage"]["estimated"] is True
        # text = "sys" + "\n" join -> len 4 -> 1 token
        assert ev.data["usage"]["input_tokens"] == 1


# ---------------------------------------------------------------------------
# C-1: judge_prompt_hash (DD-08 §5.1/§5.4)
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_hash_of_formula(self):
        lib = PromptLibrary({"judge/root_cause.v1": "t"})
        h = lib.hash_of("judge/root_cause", "glm-4.7", 0.0)
        expected = hashlib.sha256("judge/root_cause@v1|glm-4.7|0.0".encode()).hexdigest()
        assert h == expected

    def test_hash_of_no_template_version_zero(self):
        lib = PromptLibrary()
        h = lib.hash_of("judge/root_cause", "m", 0.0)
        expected = hashlib.sha256("judge/root_cause@v0|m|0.0".encode()).hexdigest()
        assert h == expected

    def test_client_prompt_hash_matches_library(self):
        lib = PromptLibrary({"judge/root_cause.v1": "t"})
        client = LLMClient(model="glm-4.7", prompt_library=lib, temperature=0.0)
        assert client.prompt_hash("judge", "root_cause") == lib.hash_of(
            "judge/root_cause", "glm-4.7", 0.0
        )

    def test_client_prompt_hash_without_library(self):
        client = LLMClient(model="m", prompt_version="v9", temperature=0.2)
        expected = hashlib.sha256("judge/root_cause@v9|m|0.2".encode()).hexdigest()
        assert client.prompt_hash("judge", "root_cause") == expected

    @pytest.mark.asyncio
    async def test_judge_run_persists_prompt_hash(self, tmp_path):
        from tests.unit.test_judge_api import _lab, _task
        from tests.unit.test_judge_medium_fixes import _Sandbox, _good_handler
        from rsi.core.models import TaskResult
        from rsi.db.tables import Judgment
        from rsi.judge.aggregate import VerdictAggregator
        from rsi.judge.antihack import AntiHackDetector
        from rsi.judge.api import JudgeService
        from rsi.judge.deterministic import DeterministicJudge
        from rsi.judge.llm_judge import LLMJudge
        from rsi.judge.safety import SafetyScanner

        engine = create_engine(f"sqlite:///{tmp_path}/c1.db")
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)

        fixed = "deadbeef" * 8
        svc = JudgeService(
            det=DeterministicJudge(),
            safety=SafetyScanner(),
            llm=LLMJudge(LLMClient(handler=_good_handler())),
            antihack=AntiHackDetector(),
            agg=VerdictAggregator(),
            session_factory=factory,
            prompt_hash_fn=lambda: fixed,
        )
        task, lab = _task(), _lab()
        events = [_ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": {"cli": "display version"}})]
        await svc.judge_run(task, TaskResult(task_complete=True), events, lab, _Sandbox(), permission="read-only")
        with factory() as s:
            rows = s.exec(select(Judgment)).all()
        assert len(rows) == 1
        assert rows[0].judge_prompt_hash == fixed

    @pytest.mark.asyncio
    async def test_judge_run_without_fn_writes_null_hash(self, tmp_path):
        from tests.unit.test_judge_api import _lab, _task
        from tests.unit.test_judge_medium_fixes import _Sandbox, _good_handler
        from rsi.core.models import TaskResult
        from rsi.db.tables import Judgment
        from rsi.judge.aggregate import VerdictAggregator
        from rsi.judge.antihack import AntiHackDetector
        from rsi.judge.api import JudgeService
        from rsi.judge.deterministic import DeterministicJudge
        from rsi.judge.llm_judge import LLMJudge
        from rsi.judge.safety import SafetyScanner

        engine = create_engine(f"sqlite:///{tmp_path}/c1b.db")
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        svc = JudgeService(
            det=DeterministicJudge(),
            safety=SafetyScanner(),
            llm=LLMJudge(LLMClient(handler=_good_handler())),
            antihack=AntiHackDetector(),
            agg=VerdictAggregator(),
            session_factory=factory,
        )
        task, lab = _task(), _lab()
        events = [_ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": {"cli": "display version"}})]
        await svc.judge_run(task, TaskResult(task_complete=True), events, lab, _Sandbox(), permission="read-only")
        with factory() as s:
            rows = s.exec(select(Judgment)).all()
        assert len(rows) == 1
        assert rows[0].judge_prompt_hash is None

    # C-7.3: empty events -> no junk run_id="" row
    @pytest.mark.asyncio
    async def test_judge_run_empty_events_skips_persist(self, tmp_path):
        from tests.unit.test_judge_api import _lab, _task
        from tests.unit.test_judge_medium_fixes import _Sandbox, _good_handler
        from rsi.core.models import TaskResult
        from rsi.db.tables import Judgment
        from rsi.judge.aggregate import VerdictAggregator
        from rsi.judge.antihack import AntiHackDetector
        from rsi.judge.api import JudgeService
        from rsi.judge.deterministic import DeterministicJudge
        from rsi.judge.llm_judge import LLMJudge
        from rsi.judge.safety import SafetyScanner

        engine = create_engine(f"sqlite:///{tmp_path}/c7.db")
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        svc = JudgeService(
            det=DeterministicJudge(),
            safety=SafetyScanner(),
            llm=LLMJudge(LLMClient(handler=_good_handler())),
            antihack=AntiHackDetector(),
            agg=VerdictAggregator(),
            session_factory=factory,
            prompt_hash_fn=lambda: "x",
        )
        task, lab = _task(), _lab()
        verdict = await svc.judge_run(task, TaskResult(task_complete=True), [], lab, _Sandbox(), permission="read-only")
        assert verdict is not None  # judging still completes
        with factory() as s:
            assert s.exec(select(Judgment)).all() == []


# ---------------------------------------------------------------------------
# C-4: CassetteStore record/replay/miss (ADR-7)
# ---------------------------------------------------------------------------


class TestCassetteStore:
    def test_replay_miss_raises(self, tmp_path):
        store = CassetteStore(tmp_path, mode="replay")
        with pytest.raises(CassetteMissError):
            store.load("missing")

    def test_append_and_load_roundtrip(self, tmp_path):
        store = CassetteStore(tmp_path, mode="record")
        store.append("cas", {"key": "k1", "content": "a"})
        store.append("cas", {"key": "k2", "content": "b"})
        assert store.keys("cas") == {"k1", "k2"}
        assert (tmp_path / "cas.ndjson").is_file()

    @pytest.mark.asyncio
    async def test_record_then_replay_hit(self, tmp_path):
        calls: list[str] = []

        def handler(component, prompt_name, vars):
            calls.append(prompt_name)
            return '{"match": true, "confidence": 0.9}'

        acc = Accountant()
        recorder = LLMClient(handler=handler, accountant=acc, cassette=CassetteStore(tmp_path, "record"))
        await recorder.complete("judge", "root_cause", {"expected": "e", "actual": "a"})
        assert len(calls) == 1
        assert (tmp_path / "default.ndjson").is_file()
        assert len(acc.records()) == 1

        # replay: handler untouched, but the hit is accounted (C-4 fix)
        player = LLMClient(handler=handler, accountant=acc, cassette=CassetteStore(tmp_path, "replay"))
        resp = await player.complete("judge", "root_cause", {"expected": "e", "actual": "a"})
        assert len(calls) == 1  # handler NOT called again
        assert resp.content == '{"match": true, "confidence": 0.9}'
        assert len(acc.records()) == 2  # replay hit recorded

    @pytest.mark.asyncio
    async def test_replay_miss_raises_cassette_miss_error(self, tmp_path):
        player = LLMClient(
            handler=lambda c, p, v: "x",
            cassette=CassetteStore(tmp_path, "replay"),
        )
        with pytest.raises(CassetteMissError):
            await player.complete("judge", "root_cause", {"expected": "e", "actual": "z"})


# ---------------------------------------------------------------------------
# C-6: frozen rule domain
# ---------------------------------------------------------------------------


class TestFrozenDomain:
    def test_freeze_verify_passes_on_repo(self):
        from rsi.judge.rules.freeze_check import verify

        assert verify() is True

    def test_freeze_detects_tamper(self, tmp_path):
        import shutil

        from rsi.judge.rules import freeze_check

        # tamper: copy manifest but alter a digest
        manifest = json.loads(freeze_check.MANIFEST_PATH.read_text(encoding="utf-8"))
        first = next(iter(manifest))
        manifest[first] = "0" * 64
        bad = tmp_path / "freeze.manifest"
        bad.write_text(json.dumps(manifest), encoding="utf-8")
        assert freeze_check.verify(bad) is False

        # tamper: manifest written for an unmodified copy, then a file changes
        tampered_root = tmp_path / "judge"
        tampered_root.mkdir()
        shutil.copytree(freeze_check.JUDGE_ROOT / "rules", tampered_root / "rules")
        shutil.copytree(freeze_check.JUDGE_ROOT / "prompts", tampered_root / "prompts")
        manifest_path = tampered_root / "rules" / "freeze.manifest"
        freeze_check.write_manifest(manifest_path, root=tampered_root)
        assert freeze_check.verify(manifest_path, root=tampered_root) is True
        target = tampered_root / first
        target.write_text(target.read_text(encoding="utf-8") + "\n# tampered", encoding="utf-8")
        assert freeze_check.verify(manifest_path, root=tampered_root) is False

    def test_yaml_rules_match_builtin_fallback(self):
        from rsi.judge.safety import _FALLBACK, load_rules

        rules, loaded = load_rules()
        assert loaded is True
        assert rules["read_only_prefixes"] == _FALLBACK["read_only_prefixes"]
        assert rules["config_delete_markers"] == _FALLBACK["config_delete_markers"]
        assert rules["config_mode_commands"] == _FALLBACK["config_mode_commands"]
        assert rules["major_patterns"] == _FALLBACK["major_patterns"]
        assert rules["minor_patterns"] == _FALLBACK["minor_patterns"]
        assert {r["id"] for r in rules["fatal"]} == {r["id"] for r in _FALLBACK["fatal"]}

    def test_missing_rules_file_falls_back(self, tmp_path):
        from rsi.judge.safety import SafetyScanner, load_rules

        rules, loaded = load_rules(tmp_path / "nope.yaml")
        assert loaded is False
        assert rules["read_only_prefixes"][0] == "display"
        # scanner still classifies with the builtin table
        scanner = SafetyScanner(rules_path=tmp_path / "nope.yaml")
        v = scanner.scan(["display version", "reset saved-configuration"], "read-only")
        assert v.fatal is True

    def test_prompt_library_loads_frozen_templates(self):
        lib = PromptLibrary(
            template_dir=Path(__file__).resolve().parents[2] / "rsi" / "judge" / "prompts",
            component="judge",
        )
        assert lib.latest_version("judge", "root_cause") == 1
        out = lib.render(
            "judge",
            "root_cause",
            {
                "task_title": "t",
                "expected": "e",
                "actual": "a",
                "confidence_of_sut": "0.5",
                "match_hints": "h",
                "eval_context": "",
            },
        )
        assert "root-cause judge" in out
        assert "t" in out


# ---------------------------------------------------------------------------
# C-7: accountant persistence + match_hints passthrough
# ---------------------------------------------------------------------------


class TestAccountantPersistence:
    def test_record_writes_llm_calls_row(self, tmp_path):
        from rsi.db.tables import LlmCall

        engine = create_engine(f"sqlite:///{tmp_path}/acc.db")
        SQLModel.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        acc = Accountant(session_factory=factory)
        acc.record("judge", "glm-4.7", "v1", 100, 40, 250, run_id="r1", batch_id="b1")
        with factory() as s:
            rows = s.exec(select(LlmCall)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.component == "judge"
        assert row.model == "glm-4.7"
        assert row.input_tokens == 100 and row.output_tokens == 40
        assert row.run_id == "r1" and row.batch_id == "b1"

    def test_record_db_failure_does_not_raise(self):
        def boom():
            raise RuntimeError("db down")

        acc = Accountant(session_factory=boom)
        acc.record("judge", "m", "v1", 1, 1, 1)  # must not raise
        assert len(acc.records()) == 1


class TestMatchHintsPassthrough:
    @pytest.mark.asyncio
    async def test_root_cause_prompt_contains_hints(self):
        from tests.unit.test_judge_api import _lab, _task
        from rsi.core.models import RootCauseExpectation, TaskResult
        from rsi.judge.llm_judge import LLMJudge
        from rsi.judge.summarizer import TraceSummarizer

        captured: dict = {}

        def handler(component, prompt_name, vars):
            captured.update(vars)
            return json.dumps({"match": True, "confidence": 0.9})

        task, lab = _task(), _lab()
        new_expected = task.task.expected.model_copy(
            update={
                "root_cause": RootCauseExpectation(
                    expected="GE0/0/1 shutdown on R1",
                    match_hints=["interface GE0/0/1", "shutdown"],
                )
            }
        )
        task = task.model_copy(
            update={"task": task.task.model_copy(update={"expected": new_expected})}
        )
        client = LLMClient(handler=handler)
        judge = LLMJudge(client, TraceSummarizer())
        await judge.judge(task, TaskResult(task_complete=True), [])
        assert "interface GE0/0/1" in captured.get("match_hints", "")
        assert "shutdown" in captured.get("match_hints", "")
