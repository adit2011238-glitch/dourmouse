"""Eval harness tests — real logic, fake backends (Integration Rule 7.3:
isolated per-file, a fake OpenAI-shaped client stands in so these run
without a real API key or network, matching test_dispatch.py's own
discipline)."""

from __future__ import annotations

import json

import pytest

from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec, Permission
from dourmouse.eval_harness import GradedResult, grade_answer, run_bench, run_question


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses):
        self.chat = _FakeChat(_FakeCompletions(responses))


def _echo_registry():
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="echo_agent", domain="Test", description="echoes text",
            tools=(
                ToolSpec(
                    name="echo",
                    description="echo",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                    handler=lambda a: f"echoed: {a.get('text', '')}",
                    permission=Permission.REGULAR,
                ),
            ),
        )
    )
    return r


class TestGradeAnswer:
    def test_parses_clean_json_response(self):
        grader = FakeClient([_FakeResponse(_FakeMessage(content=json.dumps({
            "score": 0.85, "failure_type": "correct",
            "feedback": "Real, grounded, correct.",
            "strengths": ["accurate"], "weaknesses": [],
        })))])
        result = grade_answer("q", "ideal", "real answer", ["system_info"], client=grader, model="grader-model")
        assert result["score"] == 0.85
        assert result["failure_type"] == "correct"
        assert grader.chat.completions.calls[0]["model"] == "grader-model"

    def test_falls_back_to_bare_score_regex_on_malformed_json(self):
        grader = FakeClient([_FakeResponse(_FakeMessage(
            content='some preamble text "score": 0.3 more junk not valid json {'
        ))])
        result = grade_answer("q", "ideal", "answer", [], client=grader, model="m")
        assert result["score"] == 0.3
        assert result["failure_type"] == "unknown"

    def test_neutral_score_on_total_parse_failure(self):
        grader = FakeClient([_FakeResponse(_FakeMessage(content="the grader said nothing usable"))])
        result = grade_answer("q", "ideal", "answer", [], client=grader, model="m")
        assert result["score"] == 0.5
        assert result["failure_type"] == "parse_error"

    def test_grader_call_itself_failing_is_a_real_reportable_outcome(self):
        class _BoomCompletions:
            def create(self, **kwargs):
                raise RuntimeError("backend down")

        class _BoomChat:
            completions = _BoomCompletions()

        class _BoomClient:
            chat = _BoomChat()

        result = grade_answer("q", "ideal", "answer", [], client=_BoomClient(), model="m")
        assert result["score"] == 0.0
        assert result["failure_type"] == "grader_call_failed"
        assert "backend down" in result["feedback"]

    def test_includes_real_tool_trace_in_the_grading_prompt(self):
        grader = FakeClient([_FakeResponse(_FakeMessage(content=json.dumps({"score": 1.0, "failure_type": "correct", "feedback": "", "strengths": [], "weaknesses": []})))])
        grade_answer("q", "ideal", "answer", ["system_info", "web_search"], client=grader, model="m")
        sent_prompt = grader.chat.completions.calls[0]["messages"][0]["content"]
        assert "system_info" in sent_prompt
        assert "web_search" in sent_prompt


class TestRunQuestion:
    def test_real_dispatch_run_then_graded(self):
        tool_call = _FakeToolCall("c1", "echo", json.dumps({"text": "hi"}))
        answer_client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call])),
            _FakeResponse(_FakeMessage(content="The tool echoed: hi")),
        ])
        grader_client = FakeClient([_FakeResponse(_FakeMessage(content=json.dumps({
            "score": 0.9, "failure_type": "correct", "feedback": "good",
            "strengths": [], "weaknesses": [],
        })))])
        item = {"id": "t1", "question": "echo hi", "ideal_answer": "should echo hi"}
        result = run_question(
            item, registry=_echo_registry(), answer_client=answer_client,
            grader_client=grader_client, grader_model="grader-m",
        )
        assert isinstance(result, GradedResult)
        assert result.id == "t1"
        assert "echoed: hi" in result.answer
        assert result.tool_trace == ["echo"]
        assert result.score == 0.9
        assert result.error == ""
        assert result.duration_s >= 0

    def test_a_crashed_dispatch_run_is_scored_zero_not_silently_dropped(self):
        class _BoomAnswerClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        raise RuntimeError("nvidia 429")

        item = {"id": "t2", "question": "anything", "ideal_answer": "x"}
        result = run_question(item, registry=_echo_registry(), answer_client=_BoomAnswerClient())
        assert result.score == 0.0
        assert result.failure_type == "crashed"
        assert "429" in result.error


class TestRunBench:
    def test_persists_every_result_to_the_log_even_if_a_later_one_would_crash(self, tmp_path, monkeypatch):
        log = tmp_path / "results.jsonl"

        tool_call = _FakeToolCall("c1", "echo", json.dumps({"text": "hi"}))

        def fake_run_question(item, **kwargs):
            return GradedResult(
                id=item["id"], question=item["question"], answer="a real answer",
                tool_trace=["echo"], score=0.8, failure_type="correct",
                feedback="fine", duration_s=0.1, ts=0.0,
            )

        monkeypatch.setattr("dourmouse.eval_harness.run_question", fake_run_question)
        questions = [
            {"id": "a", "question": "q1", "ideal_answer": "x"},
            {"id": "b", "question": "q2", "ideal_answer": "y"},
        ]
        results = run_bench(questions, log_path=log)
        assert len(results) == 2
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(l) for l in lines]
        assert parsed[0]["id"] == "a"
        assert parsed[1]["id"] == "b"

    def test_appends_rather_than_overwrites_existing_log(self, tmp_path, monkeypatch):
        log = tmp_path / "results.jsonl"
        log.write_text('{"id": "previous_run"}\n')

        def fake_run_question(item, **kwargs):
            return GradedResult(
                id=item["id"], question=item["question"], answer="", tool_trace=[],
                score=1.0, failure_type="correct", feedback="", ts=0.0,
            )

        monkeypatch.setattr("dourmouse.eval_harness.run_question", fake_run_question)
        run_bench([{"id": "new_run", "question": "q", "ideal_answer": "x"}], log_path=log)
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["id"] == "previous_run"
        assert json.loads(lines[1])["id"] == "new_run"


class TestQuestionBank:
    def test_every_seed_question_has_the_required_schema(self):
        from dourmouse.eval_bench import QUESTIONS

        assert len(QUESTIONS) > 0
        seen_ids = set()
        for q in QUESTIONS:
            assert q["id"] not in seen_ids, f"duplicate id {q['id']!r}"
            seen_ids.add(q["id"])
            assert q["question"].strip()
            assert q["ideal_answer"].strip()
            assert "agent" in q
