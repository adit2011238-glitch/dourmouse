"""PRIV exit test: a mailbox sample through a stubbed provider, the real dispatch loop.

(a) T3 content never appears in a request, (b) identifiers become placeholders and are mapped
back in the reply and in tool arguments, (c) a T2 turn is refused until consent, (d) the ledger
row is written with no content, (e) Private mode removes T2 tools. PRIV-1, PRIV-2, PRIV-4,
P5-31, P2-21.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dourmouse import privacy
from dourmouse.dispatch import (
    DispatchRegistry,
    OllamaNativeClient,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
)
from dourmouse.config import OllamaConfig

MAILBOX = (
    "From: Alice Example <alice@example.org>\n"
    "Subject: invoice\n"
    "Pay with card 4111 1111 1111 1111, or call me on +44 20 7946 0958.\n"
    "Ship to 221B Baker Street. The merger closes Friday."
)
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n-----END OPENSSH PRIVATE KEY-----"


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _Tc:
    def __init__(self, i: int, name: str, args: dict[str, Any]) -> None:
        self.id = f"call_{i}"
        self.function = _Fn(name, json.dumps(args))


class _Msg:
    def __init__(self, content: str | None = None, tool_calls: list[_Tc] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg: _Msg) -> None:
        self.choices = [type("C", (), {"message": msg})()]


class ScriptedProvider:
    """A stubbed cloud model: records every request it receives."""

    def __init__(self, script: list[_Msg]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs: Any) -> _Resp:
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return _Resp(self.script.pop(0) if len(self.script) > 1 else self.script[0])

    def sent_text(self) -> str:
        return json.dumps(self.requests)


def _registry(sent: list[dict[str, Any]]) -> DispatchRegistry:
    def gmail_send(a: dict[str, Any]) -> str:
        sent.append(a)
        return "SENT"

    r = DispatchRegistry()
    r.register_subagent(Subagent(
        name="mail", domain="Mail", description="reads and sends mail",
        tools=(
            ToolSpec(name="gmail_read", description="read one message",
                     parameters={"type": "object", "properties": {"id": {"type": "string"}}},
                     handler=lambda a: MAILBOX),
            ToolSpec(name="gmail_send", description="send mail",
                     parameters={"type": "object", "properties": {"to": {"type": "string"},
                                                                  "body": {"type": "string"}}},
                     handler=gmail_send),
            ToolSpec(name="read_path", description="read a file",
                     parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                     handler=lambda a: KEY),
        ),
    ))
    return r


def _run(provider: ScriptedProvider, registry: DispatchRegistry, gate: Any = None, chat: str = "tab-test") -> dict[str, Any]:
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "check the invoice mail"}]
    with privacy.chat_scope(chat):
        return run_dispatch_messages(messages, registry, client=provider, confirmation_gate=gate,
                                     forced_agent="mail", max_turns=4)


def _tool_messages(provider: ScriptedProvider) -> list[str]:
    last = provider.requests[-1]["messages"]
    return [m["content"] for m in last if m.get("role") == "tool"]


@pytest.fixture
def allow_mail():
    privacy.PrivacyStore().set_consent("mail", "allow")


def test_a_t3_key_never_appears_in_any_request(allow_mail):
    provider = ScriptedProvider([
        _Msg(tool_calls=[_Tc(1, "read_path", {"path": "~/.ssh/id_rsa"})]),
        _Msg(content="done"),
    ])
    _run(provider, _registry([]))
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in provider.sent_text()
    assert _tool_messages(provider)[0].startswith("NOT SENT: this result holds")


def test_a_t3_key_by_content_even_from_an_ordinary_path(allow_mail):
    provider = ScriptedProvider([
        _Msg(tool_calls=[_Tc(1, "read_path", {"path": "/tmp/notes.txt"})]),
        _Msg(content="done"),
    ])
    _run(provider, _registry([]))
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in provider.sent_text()
    assert _tool_messages(provider)[0].startswith("NOT SENT: this result holds a private key")


def test_b_identifiers_are_placeholders_and_map_back_in_reply_and_arguments(allow_mail):
    sent: list[dict[str, Any]] = []
    provider = ScriptedProvider([
        _Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]),
        _Msg(tool_calls=[_Tc(2, "gmail_send", {"to": "[EMAIL_1]", "body": "Paid with [CARD_1], call [PHONE_1]"})]),
        _Msg(content="Replied to [EMAIL_1] about the card [CARD_1]."),
    ])
    report = _run(provider, _registry(sent), gate=lambda _p: True)
    wire = provider.sent_text()
    for raw in ("alice@example.org", "4111 1111 1111 1111", "+44 20 7946 0958", "221B Baker Street"):
        assert raw not in wire
    assert "[EMAIL_1]" in wire and "[CARD_1]" in wire
    # Tool arguments: the real address in the recipient field only (FIX-P R5-5), never the card or the phone.
    assert sent == [{"to": "alice@example.org", "body": "Paid with [CARD_1], call [PHONE_1]"}]
    # The owner's reply shows everything.
    assert report["final_text"] == "Replied to alice@example.org about the card 4111 1111 1111 1111."
    # The model's own history keeps the placeholders.
    assert report["messages"][-1]["content"] == "Replied to [EMAIL_1] about the card [CARD_1]."


def test_c_t2_is_refused_until_consent_with_no_one_to_ask():
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]))
    assert "merger" not in provider.sent_text()
    assert _tool_messages(provider)[0].startswith("NOT SENT: the owner has not allowed Mail")
    assert "SETTINGS, PRIVACY" in _tool_messages(provider)[0]


class _OwnerGate:
    """The web gate's shape: ask_owner answers consent cards, __call__ actions."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> bool:
        return True

    def ask_owner(self, prompt: str) -> bool | None:
        self.prompts.append(prompt)
        return self.answer


def test_c_first_use_asks_once_and_remembers_yes():
    gate = _OwnerGate(True)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), gate=gate)
    assert len(gate.prompts) == 1
    assert "Mail" in gate.prompts[0] and "SETTINGS, PRIVACY" in gate.prompts[0]
    assert "merger" in provider.sent_text()
    assert privacy.PrivacyStore().consent("mail") == "allow"
    # Next turn: no second question.
    provider2 = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m2"})]), _Msg(content="ok")])
    _run(provider2, _registry([]), gate=gate)
    assert len(gate.prompts) == 1


def test_c_no_is_remembered_and_unanswered_is_not():
    gate = _OwnerGate(False)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), gate=gate)
    assert privacy.PrivacyStore().consent("mail") == "deny"
    assert "merger" not in provider.sent_text()

    privacy.PrivacyStore().set_consent("mail", "ask")
    silent = _OwnerGate(None)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), gate=silent)
    assert privacy.PrivacyStore().consent("mail") is None
    assert "merger" not in provider.sent_text()


def test_c_a_plain_callable_approves_one_request_only():
    # FIX-P R5-4: only a gate marked interactive_consent (the terminal prompt) may approve one request;
    # a plain callable (auto approve, a stub) is never asked. The old version pinned the bug.
    def terminal(_p: str) -> bool:
        return True

    terminal.interactive_consent = True  # type: ignore[attr-defined]
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), gate=terminal)
    assert "merger" in provider.sent_text()
    assert privacy.PrivacyStore().consent("mail") is None
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), gate=lambda _p: True)
    assert "merger" not in provider.sent_text()


def test_d_ledger_row_from_a_real_ollama_client_has_no_content(allow_mail):
    replies = [
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "gmail_read", "arguments": {"id": "m1"}}}]}},
        {"message": {"role": "assistant", "content": "Alice wants payment."}},
    ]
    client = OllamaNativeClient(OllamaConfig(base_url="https://ollama.com/v1", model="gpt-oss:120b",
                                             api_key="k", is_cloud=True),
                                _post=lambda payload: json.dumps(replies.pop(0)))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "check the invoice mail"}]
    with privacy.chat_scope("tab-ledger"):
        run_dispatch_messages(messages, _registry([]), client=client, forced_agent="mail", max_turns=3)
    rows = privacy.read_egress(10)
    assert len(rows) == 2
    newest = rows[0]
    assert newest["backend"] == "ollama_cloud"
    assert newest["tiers"]["T2"] == 1 and newest["sources"] == ["mail"]
    assert newest["redactions"].get("EMAIL") == 1 and newest["redactions"].get("CARD") == 1
    raw = privacy.egress_path().read_text(encoding="utf-8")
    for leaked in ("merger", "alice", "4111", "invoice", "Baker"):
        assert leaked not in raw


def test_e_private_mode_removes_t2_tools_and_refuses_a_stray_result(allow_mail):
    privacy.PrivacyStore().set_chat_private("tab-private", True)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), chat="tab-private")
    offered = {t["function"]["name"] for t in provider.requests[0].get("tools") or []}
    assert "gmail_read" not in offered and "read_path" not in offered
    assert "gmail_send" in offered
    # The model called it anyway (a stale tool list): the result is refused.
    assert "merger" not in provider.sent_text()
    assert _tool_messages(provider)[0].startswith("NOT SENT: Private mode is on")


def test_e_private_mode_is_per_chat(allow_mail):
    privacy.PrivacyStore().set_chat_private("tab-private", True)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="ok")])
    _run(provider, _registry([]), chat="tab-other")
    offered = {t["function"]["name"] for t in provider.requests[0].get("tools") or []}
    assert "gmail_read" in offered


def test_oversized_t2_is_cut_to_the_relevant_part(allow_mail):
    big = "\n\n".join([f"Paragraph {i} about gardening and weather." for i in range(800)]
                      + ["The invoice total is due on Friday."])
    r = DispatchRegistry()
    r.register_subagent(Subagent(name="mail", domain="Mail", description="mail", tools=[
        ToolSpec(name="gmail_read", description="read", parameters={"type": "object", "properties": {}},
                 handler=lambda a: big)]))
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {})]), _Msg(content="ok")])
    _run(provider, r)
    sent = _tool_messages(provider)[0]
    assert len(sent) <= privacy.T2_MAX_CHARS + 500
    assert "The invoice total is due on Friday." in sent
    assert "[PRIVACY: this result was" in sent


def test_public_results_never_ask():
    asked: list[str] = []
    gate = _OwnerGate(True)
    gate.ask_owner = lambda p: asked.append(p) or True  # type: ignore[method-assign]
    r = DispatchRegistry()
    r.register_subagent(Subagent(name="research_info", domain="Research", description="web", tools=[
        ToolSpec(name="web_search", description="search", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "result page text")]))
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "web_search", {})]), _Msg(content="ok")])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "search"}]
    run_dispatch_messages(messages, r, client=provider, confirmation_gate=gate, forced_agent="research_info",
                          max_turns=3)
    assert asked == []
    assert "result page text" in provider.sent_text()


def test_streamed_reply_shows_real_values(allow_mail):
    events: list[dict[str, Any]] = []
    replies = [
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "gmail_read", "arguments": {"id": "m1"}}}]}},
        {"message": {"role": "assistant", "content": "Mail from [EMAIL_1]."}, "done": True},
    ]
    client = OllamaNativeClient(OllamaConfig(base_url="https://ollama.com/v1", model="m", api_key="k",
                                             is_cloud=True),
                                _post=lambda payload: json.dumps(replies.pop(0)))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "check the invoice mail"}]
    with privacy.chat_scope("tab-stream"):
        run_dispatch_messages(messages, _registry([]), client=client, forced_agent="mail", max_turns=3,
                              event_sink=events.append)
    streamed = "".join(e.get("text", "") for e in events if e.get("type") == "assistant_delta")
    assert "[EMAIL_1]" not in streamed
    assert "alice@example.org" in streamed


# --------------------------------------------------------------------------- #
# Memory added without a tool call (global memory, shared memory for a CLI task)
# --------------------------------------------------------------------------- #

class _FakeMemory:
    def retrieve_context_for_prompt(self, prompt: str) -> str:
        return "RELEVANT PAST CONTEXT: the owner's private note"

    def add(self, *a: Any, **k: Any) -> None:
        pass


def test_global_memory_is_not_injected_without_memory_consent(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")
    monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
    monkeypatch.setattr("dourmouse.global_memory.get_default_memory", lambda: _FakeMemory())
    provider = ScriptedProvider([_Msg(content="ok")])
    run_dispatch_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}],
                          _registry([]), client=provider, max_turns=2)
    assert "private note" not in provider.sent_text()


def test_shared_memory_is_not_added_to_a_cli_task_without_consent(monkeypatch):
    from dourmouse import code_backends

    searched: list[str] = []

    def spy(task: str, top_k: int = 5) -> Any:
        searched.append(task)
        raise RuntimeError("not configured")

    monkeypatch.setattr("dourmouse.shared_rag.merged_search", spy)
    assert code_backends._inject_shared_context("fix the bug") == "fix the bug"
    privacy.PrivacyStore().set_consent("memory", "allow")
    privacy.PrivacyStore().set_private_mode(True)
    assert code_backends._inject_shared_context("fix the bug") == "fix the bug"
    assert searched == []
    privacy.PrivacyStore().set_private_mode(False)
    code_backends._inject_shared_context("fix the bug")
    assert searched == ["fix the bug"]


def test_h_priv4_recalled_past_turns_are_swapped_like_any_t1_result(tmp_path, monkeypatch):
    """H-PRIV-4: the owner sees real values in replies, and the session file
    (which the learn store ingests) keeps them; the store's recall block was
    then injected into the next prompt with those values raw."""
    from dourmouse import chat as chat_mod
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry
    from dourmouse.memory_store import MemoryStore

    seen: list[str] = []

    def fake_dispatch(messages, registry, **kwargs):
        seen.append(json.dumps(messages))
        messages.append({"role": "assistant", "content": "ok"})
        return {"final_text": "ok", "transcript": []}

    monkeypatch.setattr(chat_mod, "run_dispatch_messages", fake_dispatch)
    monkeypatch.setattr(chat_mod, "learn_enabled", lambda: True)
    # FIX-P R5-3: the recall block is Memory content, so it needs Memory consent (the old test pinned "T1, no consent").
    privacy.PrivacyStore().set_consent("memory", "allow")
    monkeypatch.setattr(chat_mod, "recall_block",
                        lambda memory, prompt: "REMEMBERED CONTEXT\n- carol@example.org asked about the invoice")
    session = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl", memory=MemoryStore(tmp_path / "m.db"))
    with privacy.chat_scope("tab-recall"):
        session.ask("what about the invoice")
        assert "carol@example.org" not in seen[0] and "[EMAIL_1]" in seen[0]
        assert privacy.placeholder_map("tab-recall").restore_text("[EMAIL_1]") == "carol@example.org"
