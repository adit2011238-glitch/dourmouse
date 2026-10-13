"""FIX-P R5-4: auto approve and plain confirmation callables never answer a privacy consent question."""

from __future__ import annotations

from dourmouse import goal_runtime, privacy
from dourmouse.dispatch import run_dispatch_messages


def test_plain_gates_are_not_asked():
    assert privacy.asker_for(lambda p: True) is None

    def interactive(prompt: str) -> bool:
        return True

    interactive.interactive_consent = True  # type: ignore[attr-defined]
    assert privacy.asker_for(interactive)("q") == (True, False)


def test_terminal_gate_is_marked_interactive():
    from dourmouse import dispatch

    assert dispatch._cli_confirmation_gate.interactive_consent is True


def test_goal_gate_with_auto_approve_does_not_answer_consent(monkeypatch):
    monkeypatch.setattr(goal_runtime, "auto_approve_enabled", lambda: True)
    declined: list[str] = []
    gate = goal_runtime._confirmation_gate_for((), declined)
    assert gate("Send an email?") is True  # auto approve still approves ACTIONS
    ask = privacy.asker_for(gate)
    assert ask("Send your Mail to Ollama?") == (None, False)
    assert declined == ["Send your Mail to Ollama?"]  # parked with the question


def test_goal_gate_accepts_only_the_exact_consent_prompt_the_human_saw(monkeypatch):
    monkeypatch.setattr(goal_runtime, "auto_approve_enabled", lambda: False)
    gate = goal_runtime._confirmation_gate_for(["Send your Mail to Ollama?"], [])
    assert privacy.asker_for(gate)("Send your Finance to Ollama?") == (None, False)
    assert privacy.asker_for(gate)("Send your Mail to Ollama?") == (True, True)


def test_unattended_run_with_auto_approve_cannot_read_mail(monkeypatch):
    """The whole path: a dispatch run whose gate auto approves reads a T2 source with no stored consent."""
    from dourmouse.tests.test_fix_PRIV_gate import ScriptedProvider, _Msg, _registry, _Tc

    monkeypatch.setattr(goal_runtime, "auto_approve_enabled", lambda: True)
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "1"})]), _Msg(content="done")])
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "summarise my inbox"}]
    gate = goal_runtime._confirmation_gate_for((), [])
    with privacy.chat_scope("tab-goal"):
        run_dispatch_messages(messages, _registry([]), client=provider, confirmation_gate=gate,
                              forced_agent="mail", max_turns=4)
    tool_texts = [m["content"] for m in provider.requests[-1]["messages"] if m.get("role") == "tool"]
    assert tool_texts and all("merger" not in t for t in tool_texts)
    assert privacy.PrivacyStore().consent("mail") is None
