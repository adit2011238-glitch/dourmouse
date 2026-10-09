"""FR fixes in dispatch.py (P2-2, P2-3, P2-5, ...)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dourmouse import dispatch
from dourmouse.config import NvidiaConfig

# ---- P2-2: a literal "<|" in a normal reply -------------------------------

def _filtered(chunks):
    out: list[str] = []
    f = dispatch._HarmonyDeltaFilter(out.append)
    for c in chunks:
        f.feed(c)
    f.finish()
    return "".join(out)


def test_literal_pipe_operator_is_not_swallowed():
    chunks = ["In F# you pipe with ", "x <| f", " and then continue the example.", " More text."]
    assert _filtered(chunks) == "".join(chunks)


def test_literal_pipe_at_the_very_end_is_flushed_by_finish():
    assert _filtered(["use x <| f"]) == "use x <| f"


def test_a_real_harmony_final_channel_is_still_filtered():
    text = _filtered(["<|channel|>analysis<|message|>thinking...<|end|><|start|>assistant<|channel|>final<|message|>The answer."])
    assert text == "The answer."


# ---- P2-3: retry must not replay text the user already saw ----------------

def _chunk(text):
    delta = SimpleNamespace(content=text, tool_calls=None, thinking=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


class _Client:
    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        items = self.attempts.pop(0)

        def gen():
            for it in items:
                if isinstance(it, Exception):
                    raise it
                yield _chunk(it)
        return gen()


def _cfg():
    return NvidiaConfig(base_url="http://x", model="m", api_key="k", max_retries=2, retry_backoff=0.0)


def test_failure_after_text_was_streamed_is_not_retried_and_not_replayed():
    client = _Client([["first 300 chars of a long answer. ", "and some more text to flush. ", ConnectionError("dropped")], ["whole answer"]])
    seen: list[str] = []
    with pytest.raises(ConnectionError):
        dispatch._call_with_retry_inner_impl(
            client, model="m", messages=[], tools=[], config=_cfg(), on_delta=seen.append,
        )
    assert seen and "whole answer" not in "".join(seen)
    assert client.calls == 1


def test_failure_before_any_text_is_still_retried():
    client = _Client([[ConnectionError("refused")], ["whole answer"]])
    seen: list[str] = []
    resp = dispatch._call_with_retry_inner_impl(
        client, model="m", messages=[], tools=[], config=_cfg(), on_delta=seen.append,
    )
    assert "".join(seen) == "whole answer"
    assert client.calls == 2
    assert resp.choices[0].message.content == "whole answer"


# ---- loop-level fixes: P2-4, P2-5, P2-8, P2-9, P2-12 ----------------------

from dourmouse.dispatch import (  # noqa: E402
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
)
from dourmouse.governance import DlpFilter  # noqa: E402


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Tc:
    def __init__(self, cid, name, arguments):
        self.id, self.function = cid, _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [SimpleNamespace(message=msg)]


class _Scripted:
    def __init__(self, *messages):
        self.queue = list(messages)
        self.seen = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.seen.append([dict(m) for m in kw["messages"]])
        msg = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return _Resp(msg)


def _registry(handler=None, params=None, name="probe", permission=Permission.REGULAR):
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(
        name="x", domain="d", description="d",
        tools=[ToolSpec(
            name=name, description="d",
            parameters=params or {"type": "object", "properties": {}},
            handler=handler or (lambda a: "RAN"),
            permission=permission,
            confirm_prompt=(lambda a: "ok?") if permission is Permission.REQUIRES_CONFIRMATION else None,
        )],
    ))
    return reg


def _run(reg, client, **kw):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    return run_dispatch_messages(msgs, reg, client=client, force_plain_dispatch=True, **kw)


@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "7"])
def test_non_object_tool_arguments_do_not_crash_the_turn(raw):
    client = _Scripted(_Msg(tool_calls=[_Tc("c1", "probe", raw)]), _Msg(content="done"))
    report = _run(_registry(), client)
    assert report["final_text"] == "done"
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result"]
    if raw == "null":
        assert results == ["RAN"]  # null means "no arguments"
    else:
        assert "must be a JSON object" in results[0]


def test_stop_between_tool_calls_leaves_no_unanswered_tool_call_id():
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2  # lets the between-turn check and the first tool call pass

    client = _Scripted(_Msg(tool_calls=[_Tc("a", "probe", "{}"), _Tc("b", "probe", "{}")]), _Msg(content="x"))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    run_dispatch_messages(msgs, _registry(), client=client, force_plain_dispatch=True, should_stop=stop)
    asked = [c["id"] for m in msgs if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert asked == ["a", "b"]
    assert sorted(answered) == sorted(asked)


def test_secret_in_tool_arguments_is_redacted_in_transcript_and_history():
    secret = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 3
    args = '{"text": "remember %s please"}' % secret
    seen_by_handler = []
    reg = _registry(handler=lambda a: seen_by_handler.append(a) or "RAN",
                    params={"type": "object", "properties": {"text": {"type": "string"}}})
    client = _Scripted(_Msg(tool_calls=[_Tc("c1", "probe", args)]), _Msg(content="done"))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    report = run_dispatch_messages(msgs, reg, client=client, force_plain_dispatch=True, dlp=DlpFilter())
    use = [e for e in report["transcript"] if e["type"] == "tool_use"][0]
    assert secret not in use["raw_arguments"] and "REDACTED" in use["raw_arguments"]
    assert secret not in str(msgs)
    assert secret in seen_by_handler[0]["text"]  # the real call still ran on the real value


def test_tool_exception_does_not_log_raw_arguments(monkeypatch):
    from dourmouse import obs

    logged = []
    monkeypatch.setattr(obs, "log_error", lambda **kw: logged.append(kw))
    monkeypatch.setattr(obs, "log_agent_call", lambda **kw: logged.append(kw))

    def boom(a):
        raise ValueError("upstream refused; token=sk-ant-api03-" + "Zz9Yy8Xx7W" * 4)

    spec = ToolSpec(name="t", description="d",
                    parameters={"type": "object", "properties": {"password": {"type": "string"}}},
                    handler=boom)
    out = dispatch._execute_tool_inner(spec, {"password": "hunter2-is-the-pass"}, None)
    assert out.startswith("ERROR")
    blob = repr(logged)
    assert "hunter2-is-the-pass" not in blob
    assert "sk-ant-api03-" + "Zz9Yy8Xx7W" * 4 not in blob
    err = [entry for entry in logged if "extra" in entry][0]
    assert err["extra"]["argument_names"] == ["password"] and len(err["extra"]["arguments_sha"]) == 16


def test_declined_and_confirmation_text_is_not_returned_as_the_final_answer(monkeypatch):
    for result in ("DECLINED BY USER: send?", "CONFIRMATION REQUIRED: x", "BLOCKED BY HOOK: no"):
        reg = _registry(handler=lambda a, r=result: r, name="code_claude")
        client = _Scripted(_Msg(tool_calls=[_Tc("c1", "code_claude", "{}")]), _Msg(content="model wrote this"))
        report = _run(reg, client, forced_agent="x")
        assert report["final_text"] == "model wrote this", result
    reg = _registry(handler=lambda a: "real answer", name="code_claude")
    client = _Scripted(_Msg(tool_calls=[_Tc("c1", "code_claude", "{}")]), _Msg(content="model wrote this"))
    assert _run(reg, client, forced_agent="x")["final_text"] == "real answer"


# ---- P2-6 / P2-10 / P2-11 --------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from dourmouse import model_router  # noqa: E402


def test_rotation_marks_the_account_that_actually_failed(monkeypatch):
    pool = model_router.AccountPool([
        model_router.Account("a1", "nvidia", "key-1"),
        model_router.Account("a2", "nvidia", "key-2"),
    ])
    monkeypatch.setattr(dispatch, "_get_nvidia_account_pool", lambda: pool)
    cfg = NvidiaConfig(base_url="http://x/v1", model="m", api_key="key-1")
    factory = dispatch._nvidia_rotation_factory(object(), cfg, "m")
    client, _model = factory()
    assert client.api_key == "key-2"  # not the exhausted key-1 again
    assert "a1" not in [a.name for a in pool.available()]


def test_next_turn_starts_on_an_available_account(monkeypatch):
    pool = model_router.AccountPool([
        model_router.Account("a1", "nvidia", "key-1"),
        model_router.Account("a2", "nvidia", "key-2"),
    ])
    monkeypatch.setattr(dispatch, "_get_nvidia_account_pool", lambda: pool)
    pool.mark_rate_limited("a1")  # rate limited on an earlier turn
    cfg = NvidiaConfig(base_url="http://x/v1", model="m", api_key="key-1")
    factory = dispatch._nvidia_rotation_factory(object(), cfg, "m")
    client, _ = factory.start_on_available_account()
    assert client.api_key == "key-2"
    # a healthy initial account is left alone
    pool.clear_cooldown("a1")
    factory2 = dispatch._nvidia_rotation_factory(object(), cfg, "m")
    assert factory2.start_on_available_account() is None


def test_delegate_budget_never_exceeds_its_cap_under_threads():
    ctx = dispatch.DispatchContext.__new__(dispatch.DispatchContext)
    ctx.budget = [0]
    ctx.max_delegates = 25
    granted = []

    def worker():
        for _ in range(50):
            if ctx.consume_delegate():
                granted.append(1)

    import sys
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert len(granted) == 25 and ctx.budget[0] == 25


def test_a_call_abandoned_while_queued_never_runs(monkeypatch):
    monkeypatch.setattr(dispatch, "_is_local_backend", lambda config: True)
    sem = threading.Semaphore(1)
    monkeypatch.setattr(dispatch, "_get_local_model_semaphore", lambda: sem)
    ran = []
    monkeypatch.setattr(dispatch, "_call_with_retry_inner_impl", lambda *a, **k: ran.append(1) or "late")
    monkeypatch.setattr(dispatch, "_model_call_deadline_s", lambda: 0.3)
    sem.acquire()  # another local generation holds the only slot
    with pytest.raises(dispatch.ModelCallDeadlineExceeded):
        dispatch._call_with_retry(object(), model="m", messages=[], tools=[], config=_cfg())
    sem.release()  # the orphaned worker now gets the slot...
    time.sleep(0.3)
    assert ran == []  # ...and must not run a generation nobody will read


def test_time_queued_behind_another_local_call_does_not_count_against_the_deadline(monkeypatch):
    monkeypatch.setattr(dispatch, "_is_local_backend", lambda config: True)
    sem = threading.Semaphore(1)
    monkeypatch.setattr(dispatch, "_get_local_model_semaphore", lambda: sem)
    monkeypatch.setattr(dispatch, "_call_with_retry_inner_impl", lambda *a, **k: time.sleep(0.3) or "answer")
    monkeypatch.setattr(dispatch, "_model_call_deadline_s", lambda: 0.5)
    sem.acquire()
    threading.Timer(0.3, sem.release).start()  # queued 0.3 s, then the call itself takes 0.3 s
    assert dispatch._call_with_retry(object(), model="m", messages=[], tools=[], config=_cfg()) == "answer"
