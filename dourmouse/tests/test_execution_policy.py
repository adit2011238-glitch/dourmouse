"""Finding #133 (R7): the model proposes, the runtime decides. A run
cannot loop the same call forever or bury the owner in approval prompts,
and every call is in the action ledger without its raw arguments."""

from __future__ import annotations

import json

from dourmouse import execution_policy as ep
from dourmouse.dispatch import (
    DispatchRegistry,
    Permission,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
)


class _Fn:
    def __init__(self, n, a):
        self.name, self.arguments = n, a


class _Call:
    def __init__(self, cid, n, a):
        self.id, self.function = cid, _Fn(n, json.dumps(a))


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class LoopingModel:
    """Calls the same tool with the same arguments every turn."""

    def __init__(self, name, args, turns):
        self.chat = type("C", (), {})()
        self.chat.completions = self
        self.name, self.args, self.left, self.n = name, args, turns, 0

    def create(self, **kw):
        self.n += 1
        msg = _Msg(tool_calls=[_Call(f"c{self.n}", self.name, self.args)]) if self.n <= self.left else _Msg("done")
        return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()


def _registry(ran):
    def handler(a):
        ran.append(a)
        return "ok"
    r = DispatchRegistry()
    r.register_subagent(Subagent(name="worker", domain="t", description="does things", tools=(
        ToolSpec(name="poke", description="poke", parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                 handler=handler),
        ToolSpec(name="send", description="send", parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                 handler=handler, permission=Permission.REQUIRES_CONFIRMATION, confirm_prompt=lambda a: "send?"),
    )))
    return r


def _run(registry, model, gate=None, turns=12):
    return run_dispatch_messages([{"role": "user", "content": "go"}], registry, client=model, max_turns=turns,
                                 confirmation_gate=gate, forced_agent="worker")


def test_an_identical_call_is_not_repeated_forever(monkeypatch):
    ran = []
    monkeypatch.setenv("DOURMOUSE_MAX_IDENTICAL_CALLS", "3")
    report = _run(_registry(ran), LoopingModel("poke", {"x": "same"}, 6))
    assert len(ran) <= 3
    results = [e for e in report["transcript"] if e.get("type") == "tool_result" and e.get("name") == "poke"]
    assert any("REFUSED BY POLICY" in str(r.get("text")) for r in results)


def test_a_run_cannot_bury_the_owner_in_approval_prompts(monkeypatch):
    ran, asked = [], []
    monkeypatch.setenv("DOURMOUSE_MAX_CONSEQUENTIAL_PER_RUN", "2")

    class Varying(LoopingModel):
        def create(self, **kw):
            self.args = {"x": f"v{self.n}"}  # different each time, so the loop breaker is not what stops it
            return super().create(**kw)

    _run(_registry(ran), Varying("send", {"x": "v0"}, 5), gate=lambda p: asked.append(p) or True)
    assert len(asked) == 2 and len(ran) == 2


def test_the_ledger_records_decisions_but_never_raw_arguments():
    events = []
    ep.set_action_sink(lambda kind, st, sid, actor, payload: events.append((kind, sid, actor, payload)))
    try:
        ran = []
        _run(_registry(ran), LoopingModel("send", {"x": "SECRET-TOKEN-123"}, 1), gate=lambda p: False)
    finally:
        ep.set_action_sink(None)
    kinds = [(k, sid) for k, sid, _, _ in events]
    assert ("action.proposed", "send") in kinds and ("action.declined", "send") in kinds
    assert all(actor == "worker" for _, _, actor, _ in events)
    assert "SECRET-TOKEN-123" not in json.dumps([p for *_, p in events])
    assert events[0][3]["argument_names"] == ["x"] and len(events[0][3]["arguments_sha"]) == 16


class TestOnePolicyPerRequest:
    """Finding #140: a nested run used to start a fresh policy, so the limit on
    approval-gated actions restarted at zero in every delegate and every branch."""

    def test_the_limit_holds_across_threads(self):
        import threading

        policy = ep.RunPolicy(max_identical=1000, max_consequential=50)
        allowed = []

        def worker(i: int) -> None:
            for j in range(20):
                if policy.decide("send", {"n": i * 100 + j}, consequential=True) is None:
                    allowed.append(1)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(allowed) == 50 and policy.consequential == 50

    def test_a_delegated_run_gets_the_parents_policy_not_a_new_one(self):
        from dourmouse.dispatch import (
            DispatchRegistry,
            Subagent,
            ToolSpec,
            current_dispatch_context,
            run_dispatch_messages,
        )
        from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse

        seen = []
        probe = ToolSpec(
            name="probe", description="records the policy of the run it is called from",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: seen.append(current_dispatch_context(registry).policy) or "ok",
        )
        registry = DispatchRegistry()
        registry.register_subagent(Subagent(name="p", domain="T", description="probe", tools=(probe,)))
        shared = ep.RunPolicy(max_consequential=3)
        from dourmouse.tests.test_dispatch import _FakeToolCall

        client = FakeClient([
            _FakeResponse(_FakeMessage(content=None, tool_calls=[_FakeToolCall("c1", "probe", "{}")])),
            _FakeResponse(_FakeMessage(content="done")),
        ])
        run_dispatch_messages([{"role": "user", "content": "use the probe"}], registry, client=client, policy=shared, forced_agent="p")
        assert seen == [shared]
