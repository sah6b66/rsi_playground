"""Unit tests: FinalJsonExtractor (extract_task_result)."""

from __future__ import annotations

from rsi.carrier.extract import extract_task_result


class TestFinalJsonTag:
    def test_tag_parsed(self) -> None:
        out = 'noise\n<FINAL_JSON>{"task_complete": true, "root_cause": "bfd down", "actions": [{"action": "display bfd session"}], "confidence": 0.9}</FINAL_JSON>\ntail'
        r = extract_task_result(out)
        assert r.task_complete is True
        assert r.root_cause == "bfd down"
        assert [a.action for a in r.actions] == ["display bfd session"]
        assert r.confidence == 0.9
        assert r.parse_error is False

    def test_last_tag_wins(self) -> None:
        out = (
            '<FINAL_JSON>{"task_complete": false, "root_cause": "first"}</FINAL_JSON>'
            '<FINAL_JSON>{"task_complete": true, "root_cause": "second"}</FINAL_JSON>'
        )
        r = extract_task_result(out)
        assert r.task_complete is True
        assert r.root_cause == "second"

    def test_invalid_json_in_tag_is_parse_error(self) -> None:
        r = extract_task_result("<FINAL_JSON>{not json}</FINAL_JSON>")
        assert r.task_complete is False
        assert r.parse_error is True


class TestNoTagFallback:
    def test_bare_json_object(self) -> None:
        r = extract_task_result('thinking...\n{"task_complete": true, "root_cause": "x"}')
        assert r.task_complete is True
        assert r.parse_error is False

    def test_multiple_objects_last_wins(self) -> None:
        out = '{"task_complete": false, "root_cause": "first"} then {"task_complete": true, "root_cause": "last"}'
        r = extract_task_result(out)
        assert r.task_complete is True
        assert r.root_cause == "last"

    def test_empty_stdout_is_parse_error(self) -> None:
        r = extract_task_result("")
        assert r.task_complete is False
        assert r.parse_error is True

    def test_no_json_at_all_is_parse_error(self) -> None:
        r = extract_task_result("the agent rambled without structure")
        assert r.task_complete is False
        assert r.parse_error is True

    def test_json_array_not_object_is_parse_error(self) -> None:
        r = extract_task_result("[1, 2, 3]")
        assert r.parse_error is True
