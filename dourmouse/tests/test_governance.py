"""Institutional governance tests (dourmouse/governance.py + wiring).

Covers the enterprise spec's COMPULSORY foundation:
- BudgetTracker: deterministic cost/call/wall-time capping, shared across the
  whole delegation tree, stops the run with an honest BUDGET EXHAUSTED event.
- DlpFilter: credential-shaped content redacted at the API boundary before it
  reaches the model or the transcript.
- RbacPolicy: role -> allowed tools; anything outside the role is refused
  BEFORE execution.
- Contract enforcement: tool arguments validated against the declared schema
  before any handler runs.
- Self-correction: bounded retry + backoff and optional model fallback.
- Shared truth: nested delegated runs receive the parent conversation context.

The wiring tests use a fake OpenAI-shaped client (same discipline as
test_dispatch.py): the fake shapes the LLM side; the governance layer is real.
"""

from __future__ import annotations

import json
import time

import pytest

from dourmouse.config import NvidiaConfig
from dourmouse.dispatch import (
    DispatchRegistry,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
    _call_with_retry,
    _is_transient_error,
)
from dourmouse.governance import (
    BudgetLimits,
    BudgetTracker,
    DlpFilter,
    RbacPolicy,
    validate_against_schema,
    validate_tool_arguments,
)
from dourmouse.general_roster import build_general_registry


# --- shared fake client (same shape as test_dispatch.py) ---

class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(responses))


def _echo_tool(name: str = "echo") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="echo the text back",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=lambda a: f"ECHOED: {a['text']}",
    )


# --------------------------------------------------------------------------- #
# BudgetTracker — deterministic cost-capping
# --------------------------------------------------------------------------- #

class TestBudgetTracker:
    def test_records_calls_and_tokens(self):
        b = BudgetTracker()
        b.record_call([{"role": "user", "content": "hello world"}], "short reply")
        snap = b.snapshot()
        assert snap["calls"] == 1
        assert snap["est_input_tokens"] >= 1
        assert snap["est_output_tokens"] >= 1
        assert snap["est_cost_usd"] > 0

    def test_check_none_while_within_budget(self):
        b = BudgetTracker()
        b.record_call([{"role": "user", "content": "x"}], "y")
        assert b.check() is None

    def test_call_cap_exhausts(self):
        b = BudgetTracker(BudgetLimits(max_calls=2))
        b.record_call([{"role": "user", "content": "x"}], "y")
        b.record_call([{"role": "user", "content": "x"}], "y")
        reason = b.check()
        assert reason is not None
        assert "BUDGET EXHAUSTED" in reason
        assert "2 LLM calls" in reason

    def test_cost_cap_exhausts(self):
        b = BudgetTracker(BudgetLimits(max_calls=10_000, max_est_cost_usd=0.000001))
        b.record_call([{"role": "user", "content": "a" * 1000}], "b" * 1000)
        assert "BUDGET EXHAUSTED" in b.check()

    def test_shared_tracker_is_thread_safe_snapshot(self):
        b = BudgetTracker()
        threads = [
            __import__("threading").Thread(
                target=lambda: [b.record_call([{"role": "user", "content": "x"}], "y") for _ in range(20)]
            )
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert b.snapshot()["calls"] == 80

    def test_reset_wall_clock_frees_long_lived_tracker(self):
        """A tracker born at server startup must not brick new requests.

        The web UI keeps ONE ChatSession (hence one tracker) for the whole
        life of the server; without reset_wall_clock the 600s default cap
        rejects every request once uptime passes it.
        """
        b = BudgetTracker(BudgetLimits(max_wall_seconds=1.0))
        b._started = time.monotonic() - 700.0  # simulate 700s of server uptime
        assert "BUDGET EXHAUSTED" in b.check()
        b.reset_wall_clock()
        assert b.check() is None

    def test_reset_wall_clock_keeps_calls_and_cost(self):
        """Only the wall window resets; calls/cost stay session-scoped."""
        b = BudgetTracker()
        b.record_call([{"role": "user", "content": "x" * 200}], "y" * 200)
        b._started = time.monotonic() - 5.0  # age the window so elapsed is measurable
        before = b.snapshot()
        assert before["elapsed_seconds"] >= 5.0
        b.reset_wall_clock()
        after = b.snapshot()
        assert after["calls"] == before["calls"] == 1

    def test_reset_run_frees_a_call_exhausted_tracker(self):
        """The per-request-tree envelope: a tracker that crossed the call
        cap under one directive must be usable for the next directive.

        Live-bug: the web UI keeps ONE tracker for the whole server life;
        with calls session-cumulative, ~14 directives crossed the 40-call cap
        and EVERY subsequent directive died instantly with BUDGET EXHAUSTED
        until restart. reset_run restores the full envelope per tree.
        """
        b = BudgetTracker(BudgetLimits(max_calls=40))
        for _ in range(40):
            b.record_call([{"role": "user", "content": "x"}], "y")
        assert "BUDGET EXHAUSTED" in b.check()
        b.reset_run()
        assert b.check() is None
        b.record_call([{"role": "user", "content": "x"}], "y")
        assert b.check() is None
        assert b.snapshot()["calls"] == 1


# --------------------------------------------------------------------------- #
# DlpFilter — data-loss prevention at the API boundary
# --------------------------------------------------------------------------- #

class TestDlpFilter:
    @pytest.mark.parametrize(
        "text,label",
        [
            ("key is nvapi-0123456789abcdef", "NVIDIA_API_KEY"),
            ("sk-abcdefghijklmnopqrstuvwxyz", "OPENAI_API_KEY"),
            ("AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY_ID"),
            ("-----BEGIN RSA PRIVATE KEY-----\nabc", "PRIVATE_KEY_BLOCK"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "JWT"),
            ("secret=abcdef1234567890", "SECRET_ASSIGNMENT"),
            ("ghp_abcdefghijklmnopqrstuvwxyz", "GITHUB_TOKEN"),
        ],
    )
    def test_redacts_known_secret_shapes(self, text, label):
        redacted, matched = DlpFilter().redact(text)
        assert label in matched
        assert f"[REDACTED:{label}]" in redacted
        assert text not in redacted  # nothing of the secret survives

    def test_leaves_normal_text_alone(self):
        text = "The research summary is ready and the meeting is at 3pm."
        redacted, matched = DlpFilter().redact(text)
        assert matched == []
        assert redacted == text

    def test_multiple_secrets_in_one_text(self):
        text = "ak nvapi-0123456789abcdef and also AKIAIOSFODNN7EXAMPLE"
        redacted, matched = DlpFilter().redact(text)
        assert "NVIDIA_API_KEY" in matched
        assert "AWS_ACCESS_KEY_ID" in matched
        assert "nvapi-0123456789abcdef" not in redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted


# --------------------------------------------------------------------------- #
# RbacPolicy — role-based access control
# --------------------------------------------------------------------------- #

class TestRbacPolicy:
    def test_operator_allows_everything(self):
        p = RbacPolicy("operator")
        assert p.allows("write_file")
        assert p.allows("run_privileged_command")
        assert p.allows("anything_at_all")

    def test_readonly_blocks_mutations(self):
        p = RbacPolicy("readonly")
        assert p.allows("web_search")
        assert p.allows("read_file")
        assert p.allows("list_files")
        assert not p.allows("write_file")
        assert not p.allows("delete_file")
        assert not p.allows("run_command")
        assert not p.allows("send_draft")
        assert not p.allows("delegate_task")

    def test_readonly_refusal_text_is_loud(self):
        p = RbacPolicy("readonly")
        text = p.refusal_text("write_file")
        assert "REFUSED by RBAC policy" in text
        assert "'write_file'" in text
        assert "readonly" in text

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError, match="unknown RBAC role"):
            RbacPolicy("superuser")

    def test_custom_allow_set(self):
        p = RbacPolicy("custom", allow={"web_search"})
        assert p.allows("web_search")
        assert not p.allows("fetch_url")
        assert p.snapshot()["role"] == "custom"


# --------------------------------------------------------------------------- #
# Contract enforcement — schema validation
# --------------------------------------------------------------------------- #

class TestSchemaValidation:
    SCHEMA = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }

    def test_valid_arguments_pass(self):
        assert validate_tool_arguments(self.SCHEMA, {"query": "nvidia", "max_results": 5}) is None

    def test_missing_required_fails(self):
        err = validate_tool_arguments(self.SCHEMA, {"max_results": 5})
        assert err is not None
        assert "missing required argument 'query'" in err

    def test_wrong_type_fails(self):
        err = validate_tool_arguments(self.SCHEMA, {"query": "x", "max_results": "many"})
        assert err is not None
        assert "max_results" in err and "integer" in err

    def test_extra_arguments_tolerated(self):
        assert validate_tool_arguments(self.SCHEMA, {"query": "x", "bogus": True}) is None


class TestNumericStringCoercion:
    """v8.12: observed live — web_search's max_results sent as the JSON
    string "10" instead of the integer 10, rejected 3x verbatim before the
    model gave up and dropped the argument. A numeric-looking string is a
    known tool-calling quirk, not malformed input, so it is coerced and
    re-checked rather than rejected outright. Genuinely non-numeric strings
    ("many", "abc") must still be rejected exactly as before."""

    INT_SCHEMA = {
        "type": "object",
        "properties": {"max_results": {"type": "integer"}},
        "required": [],
    }
    NUM_SCHEMA = {
        "type": "object",
        "properties": {"amount": {"type": "number"}},
        "required": [],
    }

    def test_numeric_string_for_integer_field_is_accepted(self):
        assert validate_tool_arguments(self.INT_SCHEMA, {"max_results": "10"}) is None

    def test_numeric_string_is_coerced_to_a_real_int_in_place(self):
        """The handler receives what it actually asked for, not the
        original string — the whole point is the handler never has to
        special-case this."""
        args = {"max_results": "10"}
        assert validate_tool_arguments(self.INT_SCHEMA, args) is None
        assert args["max_results"] == 10
        assert isinstance(args["max_results"], int)

    def test_negative_numeric_string_is_accepted(self):
        args = {"max_results": "-3"}
        assert validate_tool_arguments(self.INT_SCHEMA, args) is None
        assert args["max_results"] == -3

    def test_float_shaped_string_is_rejected_for_an_integer_field(self):
        """"3.5" must not be silently truncated into a real integer — a
        wrong-shaped value stays rejected, exactly as before."""
        err = validate_tool_arguments(self.INT_SCHEMA, {"max_results": "3.5"})
        assert err is not None

    def test_non_numeric_string_is_still_rejected(self):
        err = validate_tool_arguments(self.INT_SCHEMA, {"max_results": "many"})
        assert err is not None
        assert "max_results" in err and "integer" in err

    def test_numeric_string_for_number_field_is_coerced_to_float(self):
        args = {"amount": "3.5"}
        assert validate_tool_arguments(self.NUM_SCHEMA, args) is None
        assert args["amount"] == 3.5
        assert isinstance(args["amount"], float)

    def test_a_real_int_is_never_touched(self):
        """The already-correct, common path must be a no-op — no needless
        float/int churn on a value that was already right."""
        args = {"max_results": 10}
        assert validate_tool_arguments(self.INT_SCHEMA, args) is None
        assert args["max_results"] == 10
        assert type(args["max_results"]) is int

    def test_boolean_is_not_coerced_as_a_numeric_string(self):
        """Coercion is gated on ``isinstance(value, str)``, and Python's
        bool is an int subclass but never a str -- guards against a future
        refactor accidentally routing True/False through this path."""
        err = validate_tool_arguments(self.INT_SCHEMA, {"max_results": True})
        assert err is not None

    def test_validate_against_schema_output(self):
        schema = {"type": "object", "properties": {"symbols": {"type": "array"}}, "required": ["symbols"]}
        assert validate_against_schema({"symbols": ["SPY"]}, schema) is None
        err = validate_against_schema({"nope": 1}, schema)
        assert err is not None and "symbols" in err


# --------------------------------------------------------------------------- #
# Self-correction — retry/backoff + model fallback
# --------------------------------------------------------------------------- #

class _FlakyCompletions:
    """Fails the first N creates with a (patched-as-transient) exception."""

    def __init__(self, fail_times, response):
        self.fail_times = fail_times
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("rate limited")
        return self.response


class _FlakyChat:
    def __init__(self, completions):
        self.completions = completions


class _FlakyClient:
    def __init__(self, fail_times, response):
        self.chat = _FlakyChat(_FlakyCompletions(fail_times, response))


class TestRetryAndFallback:
    def test_transient_error_is_retried_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("dourmouse.dispatch._is_transient_error", lambda exc: True)
        config = NvidiaConfig(
            api_key="k", base_url="u", model="primary", max_retries=2, retry_backoff=0.01
        )
        client = _FlakyClient(2, _FakeResponse(_FakeMessage(content="ok")))
        result = _call_with_retry(
            client, model="primary", messages=[{"role": "user", "content": "x"}],
            tools=[], config=config,
        )
        assert result.choices[0].message.content == "ok"
        assert len(client.chat.completions.calls) == 3  # 2 fails + 1 success

    def test_non_transient_error_is_not_retried(self):
        # _is_transient_error is NOT patched: RuntimeError is not an OpenAI
        # transient class, so it must propagate immediately (never masked).
        config = NvidiaConfig(api_key="k", base_url="u", model="primary", max_retries=5)
        client = _FlakyClient(99, _FakeResponse(_FakeMessage(content="x")))
        with pytest.raises(RuntimeError, match="rate limited"):
            _call_with_retry(
                client, model="primary", messages=[], tools=[], config=config
            )
        assert len(client.chat.completions.calls) == 1  # no retries for auth-style errors

    def test_fallback_model_used_after_retries_exhausted(self, monkeypatch):
        monkeypatch.setattr("dourmouse.dispatch._is_transient_error", lambda exc: True)
        config = NvidiaConfig(
            api_key="k", base_url="u", model="primary", max_retries=2,
            retry_backoff=0.01, fallback_model="backup-model",
        )
        client = _FlakyClient(3, _FakeResponse(_FakeMessage(content="from backup")))
        result = _call_with_retry(
            client, model="primary", messages=[], tools=[], config=config
        )
        assert result.choices[0].message.content == "from backup"
        models = [c["model"] for c in client.chat.completions.calls]
        assert models == ["primary", "primary", "primary", "backup-model"]

    def test_is_transient_error_classification(self):
        import openai

        assert not _is_transient_error(RuntimeError("boom"))
        # Real OpenAI transient classes are recognized. RateLimitError's
        # constructor needs a response with .request, .status_code and
        # .headers (openai v2 shape: APIStatusError reads x-request-id).
        class _FakeRequest:
            pass

        class _FakeResponse:
            request = _FakeRequest()
            status_code = 429
            headers = {"x-request-id": "req_1"}  # plain dict has .get

        transient = openai.RateLimitError("rl", response=_FakeResponse(), body=None)
        assert _is_transient_error(transient)
        # Non-transient OpenAI errors (e.g. auth) must NOT be retried.
        auth = openai.AuthenticationError(
            "bad key", response=_FakeResponse(), body=None
        )
        assert not _is_transient_error(auth)


# --------------------------------------------------------------------------- #
# Wiring through the real engine — budget, DLP, RBAC, schema, parent context
# --------------------------------------------------------------------------- #

def _echo_registry() -> DispatchRegistry:
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(name="echo_agent", domain="Test", description="echoes", tools=(_echo_tool(),))
    )
    return r


class TestEngineWiring:
    def test_budget_cap_stops_runaway_loop(self):
        """A model that never stops calling tools is halted by the budget."""
        tool_call = _FakeToolCall("c1", "echo", json.dumps({"text": "x"}))
        looping = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        client = FakeClient([looping])
        budget = BudgetTracker(BudgetLimits(max_calls=2))
        report = run_dispatch_messages(
            [{"role": "user", "content": "loop"}],
            _echo_registry(),
            client=client,
            max_turns=20,  # would loop forever without the budget
            cost_budget=budget,
        )
        assert report["transcript"][-1]["type"] == "budget_exhausted"
        assert "BUDGET EXHAUSTED" in report["transcript"][-1]["reason"]
        assert len(client.chat.completions.calls) == 2  # capped, not 20

    def test_dlp_redacts_tool_result_at_boundary(self):
        """A tool returning a credential is redacted before the model sees it."""
        leak_tool = ToolSpec(
            name="leak",
            description="returns a secret",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: "the key is nvapi-0123456789abcdef",
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="leaky", domain="Test", description="x", tools=(leak_tool,)))
        tool_call = _FakeToolCall("c1", "leak", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="got it"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "run leak"}],
            r,
            client=client,
            dlp=DlpFilter(),
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "nvapi-0123456789abcdef" not in result["text"]
        assert "[REDACTED:NVIDIA_API_KEY]" in result["text"]
        assert "secret pattern(s) redacted" in result["text"]
        # The model's next request must not contain the secret either.
        sent = json.dumps(client.chat.completions.calls[1]["messages"])
        assert "nvapi-0123456789abcdef" not in sent

    def test_dlp_redacts_model_text_before_transcript(self):
        r = _echo_registry()
        client = FakeClient(
            [_FakeResponse(_FakeMessage(content="the api_key=abcdef1234567890xyz is safe now"))]
        )
        report = run_dispatch_messages(
            [{"role": "user", "content": "hi"}],
            r,
            client=client,
            dlp=DlpFilter(),
        )
        assert "abcdef1234567890xyz" not in report["final_text"]
        assert "REDACTED" in report["final_text"]

    def test_rbac_readonly_refuses_before_execution(self):
        r = _echo_registry()
        tool_call = _FakeToolCall("c1", "echo", json.dumps({"text": "secret"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "echo"}],
            r,
            client=client,
            rbac=RbacPolicy("readonly"),
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "REFUSED by RBAC policy" in result["text"]
        assert "ECHOED" not in result["text"]  # handler never ran

    def test_operator_role_executes_normally(self):
        r = _echo_registry()
        tool_call = _FakeToolCall("c1", "echo", json.dumps({"text": "hi"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "echo"}],
            r,
            client=client,
            rbac=RbacPolicy("operator"),
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "ECHOED: hi" in result["text"]

    def test_schema_validation_blocks_bad_arguments_before_handler(self):
        """The engine validates arguments against the declared schema: a wrong
        type is rejected and the handler is never called."""
        calls = []
        spec = ToolSpec(
            name="typed",
            description="needs an int",
            parameters={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            handler=lambda a: calls.append(a) or "RAN",
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="t", domain="Test", description="x", tools=(spec,)))
        tool_call = _FakeToolCall("c1", "typed", json.dumps({"count": "not-a-number"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            r,
            client=client,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "invalid arguments" in result["text"]
        assert "count" in result["text"] and "integer" in result["text"]
        assert calls == []  # handler never ran

    def test_missing_required_argument_rejected(self):
        r = _echo_registry()
        tool_call = _FakeToolCall("c1", "echo", json.dumps({}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "echo"}],
            r,
            client=client,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "missing required argument 'text'" in result["text"]

    def test_output_schema_valid_json_passes(self):
        """A tool that declares an output_schema and returns valid JSON rides
        through unchanged (contract enforcement, spec: structured output)."""
        spec = ToolSpec(
            name="json_tool",
            description="returns JSON",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: json.dumps({"symbols": ["SPY", "QQQ"]}),
            output_schema={
                "type": "object",
                "properties": {"symbols": {"type": "array"}},
                "required": ["symbols"],
            },
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="j", domain="Test", description="x", tools=(spec,)))
        tool_call = _FakeToolCall("c1", "json_tool", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            r,
            client=client,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "OUTPUT CONTRACT VIOLATION" not in result["text"]
        assert '"SPY"' in result["text"]

    def test_output_schema_violation_surfaced_honestly(self):
        """A declared output_schema that the handler violates is surfaced in
        the result — never silently passed to the next agent (Rule 2.2)."""
        spec = ToolSpec(
            name="bad_json_tool",
            description="returns malformed JSON",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: json.dumps({"nope": 1}),
            output_schema={
                "type": "object",
                "properties": {"symbols": {"type": "array"}},
                "required": ["symbols"],
            },
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="j", domain="Test", description="x", tools=(spec,)))
        tool_call = _FakeToolCall("c1", "bad_json_tool", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            r,
            client=client,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "OUTPUT CONTRACT VIOLATION" in result["text"]
        assert "symbols" in result["text"]

    def test_output_schema_non_json_surfaced_honestly(self):
        spec = ToolSpec(
            name="text_tool",
            description="returns plain text despite schema",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: "just some text",
            output_schema={"type": "object", "properties": {}},
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="j", domain="Test", description="x", tools=(spec,)))
        tool_call = _FakeToolCall("c1", "text_tool", "{}")
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="ok"))
        client = FakeClient([first, second])
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            r,
            client=client,
        )
        result = next(t for t in report["transcript"] if t["type"] == "tool_result")
        assert "non-JSON" in result["text"]

    def test_nested_delegate_receives_parent_context(self):
        """Delegated runs inherit the parent conversation's context, so nested
        agents hold consistent truth (spec: state management & memory)."""
        registry = build_general_registry()
        registry.register_subagent(
            Subagent(name="echo_agent", domain="Test", description="echoes", tools=(_echo_tool(),))
        )
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "confirm", "subagent": "echo_agent"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="nested ok")),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [
                {"role": "user", "content": "remember: budget number is 42"},
                {"role": "assistant", "content": "Noted."},
                {"role": "user", "content": "delegate this"},
            ],
            registry,
            client=client,
        )
        assert report["final_text"] == "parent ok"
        # The nested run's first LLM request carries the parent context.
        nested_request = client.chat.completions.calls[1]
        joined = json.dumps(nested_request["messages"])
        assert "PARENT CONTEXT" in joined
        assert "budget number is 42" in joined

    def test_budget_shared_across_nested_tree(self):
        """A tiny tree-wide budget stops the whole tree, not just one level.

        Sequence with max_calls=2: parent call 1 (delegate) -> record (1);
        nested call (answer) -> record (2); parent iteration 2 checks and is
        EXHAUSTED before its third call. Both levels share one tracker."""
        registry = build_general_registry()
        registry.register_subagent(
            Subagent(name="echo_agent", domain="Test", description="echoes", tools=(_echo_tool(),))
        )
        budget = BudgetTracker(BudgetLimits(max_calls=2))
        responses = [
            _FakeResponse(
                _FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall("c1", "delegate_task", json.dumps({"task": "go", "subagent": "echo_agent"}))],
                )
            ),
            _FakeResponse(_FakeMessage(content="nested ok")),
            _FakeResponse(_FakeMessage(content="parent ok")),
        ]
        client = FakeClient(responses)
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            registry,
            client=client,
            cost_budget=budget,
        )
        # Parent 1 + nested 1 = 2 calls; the parent's 3rd call never happens.
        assert client.chat.completions.calls.__len__() == 2
        assert report["transcript"][-1]["type"] == "budget_exhausted"
        assert budget.snapshot()["calls"] == 2


# --------------------------------------------------------------------------- #
# Intervention ledger — human decisions land in the transcript
# --------------------------------------------------------------------------- #

class TestInterventionLedger:
    def test_gated_tool_records_confirmation_events(self):
        gated = ToolSpec(
            name="gated_echo",
            description="gated",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=lambda a: f"ECHOED: {a['text']}",
            permission=__import__("dourmouse.dispatch", fromlist=["Permission"]).Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: f"Echo {a['text']!r}?",
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="g", domain="Test", description="x", tools=(gated,)))
        tool_call = _FakeToolCall("c1", "gated_echo", json.dumps({"text": "go"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="done"))
        client = FakeClient([first, second])
        decisions = []
        report = run_dispatch_messages(
            [{"role": "user", "content": "go"}],
            r,
            client=client,
            confirmation_gate=lambda t: decisions.append(t) or True,
        )
        types = [e["type"] for e in report["transcript"]]
        assert "confirmation_requested" in types
        assert "confirmation_resolved" in types
        requested = next(e for e in report["transcript"] if e["type"] == "confirmation_requested")
        resolved = next(e for e in report["transcript"] if e["type"] == "confirmation_resolved")
        assert requested["tool"] == "gated_echo"
        assert requested["prompt"] == "Echo 'go'?"
        assert resolved["approved"] is True
        assert decisions == ["Echo 'go'?"]


# --------------------------------------------------------------------------- #
# Immutable audit trail — hash-chained session ledger + latency + interventions
# --------------------------------------------------------------------------- #

class TestAuditLedger:
    def _session(self, tmp_path, monkeypatch, responses):
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        session_file = tmp_path / "sess" / "s1.jsonl"
        r = _echo_registry()
        return ChatSession(r, session_file=session_file, client=FakeClient(responses)), session_file

    def test_records_are_hash_chained_and_verify_ok(self, tmp_path, monkeypatch):
        from dourmouse.chat import verify_session_audit

        session, path = self._session(
            tmp_path, monkeypatch,
            [_FakeResponse(_FakeMessage(content="first reply"))],
        )
        session.ask("hello one")
        session.ask("hello two")

        ok, errors = verify_session_audit(path)
        assert ok, errors
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        # Chain: record 2's prev_hash == record 1's hash.
        assert lines[1]["prev_hash"] == lines[0]["hash"]
        assert lines[0]["prev_hash"] is None
        # Latency metric present (spec: latency metrics logged).
        assert lines[0]["elapsed_ms"] >= 0
        # Record hash covers everything except the hash itself.
        assert lines[0]["hash"] == __import__("dourmouse.chat", fromlist=["_record_hash"])._record_hash(lines[0])

    def test_edit_to_record_is_detected(self, tmp_path, monkeypatch):
        """A content edit breaks the record's own hash (its stored hash no
        longer matches the recomputed one)."""
        from dourmouse.chat import verify_session_audit

        session, path = self._session(
            tmp_path, monkeypatch,
            [_FakeResponse(_FakeMessage(content="first")), _FakeResponse(_FakeMessage(content="second"))],
        )
        session.ask("one")
        session.ask("two")
        lines = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["final_text"] = "tampered!"
        lines[0] = json.dumps(first)
        path.write_text("\n".join(lines) + "\n")
        ok, errors = verify_session_audit(path)
        assert not ok
        assert any("hash mismatch" in e for e in errors)

    def test_deleted_record_breaks_chain(self, tmp_path, monkeypatch):
        """Removing a record leaves the next record's prev_hash pointing at a
        hash that no longer exists — the chain link is broken."""
        from dourmouse.chat import verify_session_audit

        session, path = self._session(
            tmp_path, monkeypatch,
            [_FakeResponse(_FakeMessage(content="first")), _FakeResponse(_FakeMessage(content="second"))],
        )
        session.ask("one")
        session.ask("two")
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        path.write_text(lines[1] + "\n")  # drop the first record entirely
        ok, errors = verify_session_audit(path)
        assert not ok
        assert any("prev_hash broken" in e for e in errors)

    def test_interventions_and_latency_persisted(self, tmp_path, monkeypatch):
        from dourmouse.dispatch import Permission

        gated = ToolSpec(
            name="gated_echo",
            description="gated",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=lambda a: f"ECHOED: {a['text']}",
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: f"Echo {a['text']!r}?",
        )
        r = DispatchRegistry()
        r.register_subagent(Subagent(name="g", domain="Test", description="x", tools=(gated,)))
        tool_call = _FakeToolCall("c1", "gated_echo", json.dumps({"text": "go"}))
        first = _FakeResponse(_FakeMessage(content=None, tool_calls=[tool_call]))
        second = _FakeResponse(_FakeMessage(content="done"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        from dourmouse.chat import ChatSession

        session = ChatSession(
            r,
            session_file=tmp_path / "s" / "s1.jsonl",
            client=FakeClient([first, second]),
            confirmation_gate=lambda t: True,
        )
        session.ask("go")
        rec = json.loads(session.session_file.read_text(encoding="utf-8").splitlines()[0])
        assert len(rec["interventions"]) == 2
        assert rec["interventions"][0]["type"] == "confirmation_requested"
        assert rec["interventions"][1]["approved"] is True

    def test_resume_chains_onto_previous_hash(self, tmp_path, monkeypatch):
        from dourmouse.chat import ChatSession, verify_session_audit

        session, path = self._session(
            tmp_path, monkeypatch,
            [_FakeResponse(_FakeMessage(content="first"))],
        )
        session.ask("one")
        # A NEW session on the SAME file must continue the chain.
        r = _echo_registry()
        resumed = ChatSession(
            r, session_file=path, client=FakeClient([_FakeResponse(_FakeMessage(content="second"))])
        )
        resumed.ask("two")
        ok, errors = verify_session_audit(path)
        assert ok, errors
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines[1]["prev_hash"] == lines[0]["hash"]


# --------------------------------------------------------------------------- #
# /api/budget + DOURMOUSE_ROLE wiring
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("DOURMOUSE_ROLE", raising=False)
    import threading

    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestBudgetEndpoint:
    def test_budget_endpoint_returns_snapshot_and_role(self, server):
        import http.client

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/budget")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert body["budget"]["calls"] == 0
        assert body["budget"]["limits"]["max_calls"] > 0
        assert body["rbac"]["role"] == "operator"

    def test_readonly_role_from_env(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_ROLE", "readonly")
        from dourmouse.webui import run_server

        srv = run_server(build_general_registry(), port=0, client=None, config=None)
        try:
            assert srv.session.rbac.snapshot()["role"] == "readonly"
            assert not srv.session.rbac.allows("write_file")
        finally:
            srv.server_close()

    def test_unknown_role_raises_at_startup(self, monkeypatch):
        """An invalid DOURMOUSE_ROLE fails loudly BEFORE any socket is bound.

        Patches the exact name webui.py uses (it imports ThreadingHTTPServer
        directly) so a bind would be visible; run_server validates the role
        before constructing the server, so no bind ever happens."""
        monkeypatch.setenv("DOURMOUSE_ROLE", "superuser")
        import dourmouse.webui as webui_module

        bound = []

        def _no_bind(*a, **k):
            bound.append(True)
            raise AssertionError("server must not bind with an invalid role")

        monkeypatch.setattr(webui_module, "ThreadingHTTPServer", _no_bind)
        with pytest.raises(ValueError, match="DOURMOUSE_ROLE invalid"):
            webui_module.run_server(
                build_general_registry(), port=0, client=None, config=None
            )
        assert bound == []  # role validation raised before any bind


# --------------------------------------------------------------------------- #
# Phase A2 — audit export
# --------------------------------------------------------------------------- #

class TestAuditExport:
    def _session(self, tmp_path, monkeypatch):
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        r = _echo_registry()
        return ChatSession(
            r,
            session_file=tmp_path / "sess" / "s1.jsonl",
            client=FakeClient(
                [
                    _FakeResponse(_FakeMessage(content="first")),
                    _FakeResponse(_FakeMessage(content="second")),
                ]
            ),
        )

    def test_export_copies_intact_ledger(self, tmp_path, monkeypatch):
        from dourmouse.chat import export_audit

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        session.ask("two")
        out = tmp_path / "export" / "ledger.jsonl"
        ok, errors = export_audit(session.session_file, out)
        assert ok, errors
        assert out.read_text(encoding="utf-8") == session.session_file.read_text(encoding="utf-8")

    def test_export_refuses_tampered_ledger(self, tmp_path, monkeypatch):
        """A broken chain is never propagated to the compliance export."""
        from dourmouse.chat import export_audit

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        session.ask("two")
        lines = session.session_file.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["final_text"] = "tampered!"
        lines[0] = json.dumps(first)
        session.session_file.write_text("\n".join(lines) + "\n")
        out = tmp_path / "export" / "ledger.jsonl"
        ok, errors = export_audit(session.session_file, out)
        assert not ok
        assert any("hash mismatch" in e for e in errors)
        assert not out.exists()  # nothing was exported

    def test_export_creates_parent_dirs(self, tmp_path, monkeypatch):
        from dourmouse.chat import export_audit

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        deep = tmp_path / "a" / "b" / "c" / "ledger.jsonl"
        ok, errors = export_audit(session.session_file, deep)
        assert ok, errors
        assert deep.exists()

    # -- CLI entry point: the scriptable exit-code contract ------------- #

    def test_cli_verify_returns_zero_on_intact(self, tmp_path, monkeypatch, capsys):
        """`--verify` exits 0 on an intact chain — the scripting contract."""
        from dourmouse.chat import main

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        session.ask("two")
        assert main(["--verify", str(session.session_file)]) == 0
        assert "VERIFIED" in capsys.readouterr().out

    def test_cli_verify_returns_one_on_tampered(self, tmp_path, monkeypatch, capsys):
        """`--verify` exits 1 on a broken chain — never silently passes."""
        from dourmouse.chat import main

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        lines = session.session_file.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["final_text"] = "tampered!"
        lines[0] = json.dumps(first)
        session.session_file.write_text("\n".join(lines) + "\n")
        assert main(["--verify", str(session.session_file)]) == 1
        out = capsys.readouterr().out
        assert "TAMPERED" in out

    def test_cli_export_writes_verified_ledger(self, tmp_path, monkeypatch, capsys):
        from dourmouse.chat import main

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        out = tmp_path / "export" / "cli.jsonl"
        assert main(["--export", str(session.session_file), str(out)]) == 0
        assert out.exists()
        assert "EXPORTED" in capsys.readouterr().out

    def test_cli_export_refuses_tampered(self, tmp_path, monkeypatch, capsys):
        """A broken ledger is never exported, and the CLI says so loudly."""
        from dourmouse.chat import main

        session = self._session(tmp_path, monkeypatch)
        session.ask("one")
        lines = session.session_file.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["final_text"] = "tampered!"
        lines[0] = json.dumps(first)
        session.session_file.write_text("\n".join(lines) + "\n")
        out = tmp_path / "export" / "cli.jsonl"
        assert main(["--export", str(session.session_file), str(out)]) == 1
        assert not out.exists()
        assert "REFUSED" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Phase A3 — per-conversation role switch
# --------------------------------------------------------------------------- #

class TestRoleSwitch:
    def _session(self, tmp_path, monkeypatch):
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        return ChatSession(
            _echo_registry(),
            session_file=tmp_path / "s" / "s1.jsonl",
            client=FakeClient([]),
        )

    def test_set_role_switches_and_audits(self, tmp_path, monkeypatch):
        session = self._session(tmp_path, monkeypatch)
        assert session.rbac.snapshot()["role"] == "operator"
        snap = session.set_role("readonly")
        assert snap["role"] == "readonly"
        assert not session.rbac.allows("write_file")
        # Audited event with prior role + timestamp (self-contained ledger).
        assert len(session.role_changes) == 1
        assert session.role_changes[0]["from"] == "operator"
        assert session.role_changes[0]["role"] == "readonly"
        assert session.role_changes[0]["at"]

    def test_set_role_invalid_raises_and_is_not_recorded(self, tmp_path, monkeypatch):
        session = self._session(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="unknown RBAC role"):
            session.set_role("superuser")
        assert session.rbac.snapshot()["role"] == "operator"  # unchanged
        assert session.role_changes == []  # attempted switch NOT recorded

    def test_role_switch_is_per_conversation(self, tmp_path, monkeypatch):
        from dourmouse.chat import ChatSession

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        a = ChatSession(
            _echo_registry(),
            session_file=tmp_path / "s" / "a.jsonl",
            client=FakeClient([]),
        )
        b = ChatSession(
            _echo_registry(),
            session_file=tmp_path / "s" / "b.jsonl",
            client=FakeClient([]),
        )
        a.set_role("readonly")
        assert a.rbac.snapshot()["role"] == "readonly"
        assert b.rbac.snapshot()["role"] == "operator"  # untouched


class TestRoleEndpoint:
    def _post(self, server, role):
        import http.client

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/role",
            body=json.dumps({"role": role}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def test_role_switch_ok(self, server):
        status, body = self._post(server, "readonly")
        assert status == 200
        assert body["rbac"]["role"] == "readonly"

    def test_invalid_role_400(self, server):
        status, body = self._post(server, "superuser")
        assert status == 400
        assert "unknown RBAC role" in body["error"]

    def test_missing_role_400(self, server):
        import http.client

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/role",
            body=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 400
        assert "role is required" in body["error"]

    def test_readonly_app_role_refuses_elevation(self, monkeypatch):
        """A readonly deployment cannot self-elevate via the API; the app
        role is the ceiling. Same-role reassertion stays allowed."""
        monkeypatch.setenv("DOURMOUSE_ROLE", "readonly")
        import threading
        from dourmouse.webui import run_server

        srv = run_server(build_general_registry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        try:
            status, body = self._post((srv, port), "operator")
            assert status == 403
            assert "would exceed" in body["error"]
            assert srv.session.rbac.snapshot()["role"] == "readonly"  # unchanged
            status, _ = self._post((srv, port), "readonly")
            assert status == 200  # same-role reassertion is fine
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
