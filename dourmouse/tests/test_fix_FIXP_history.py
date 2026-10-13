"""FIX-P R5-9: turning Private mode on (or a source to NEVER) removes what the chat already holds."""

from __future__ import annotations

from typing import Any

from dourmouse import privacy
from dourmouse.dispatch import run_dispatch_messages
from dourmouse.tests.test_fix_PRIV_gate import ScriptedProvider, _Msg, _registry, _Tc


def _first_turn(chat: str) -> list[dict[str, Any]]:
    privacy.PrivacyStore().set_consent("mail", "allow")
    provider = ScriptedProvider([_Msg(tool_calls=[_Tc(1, "gmail_read", {"id": "m1"})]), _Msg(content="The merger closes Friday.")])
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "check the invoice mail"}]
    with privacy.chat_scope(chat):
        run_dispatch_messages(messages, _registry([]), client=provider, forced_agent="mail", max_turns=4)
    assert "merger" in provider.sent_text()
    return messages


def _second_turn(messages: list[dict[str, Any]], chat: str) -> ScriptedProvider:
    provider = ScriptedProvider([_Msg(content="ok")])
    messages.append({"role": "user", "content": "and now something else"})
    with privacy.chat_scope(chat):
        run_dispatch_messages(messages, _registry([]), client=provider, forced_agent="mail", max_turns=4)
    return provider


def test_private_mode_drops_earlier_sensitive_results_and_answers():
    messages = _first_turn("tab-h1")
    tagged = [m for m in messages if m.get(privacy.PRIV_TAG)]
    assert {m["role"] for m in tagged} == {"tool", "assistant"}  # the tool result and the answer written from it
    privacy.PrivacyStore().set_chat_private("tab-h1", True)
    provider = _second_turn(messages, "tab-h1")
    sent = provider.sent_text()
    assert "merger" not in sent and "Pay with card" not in sent
    assert "removed from this chat's context" in sent
    assert privacy.PRIV_TAG not in sent  # bookkeeping never reaches a provider


def test_without_private_mode_history_is_kept_and_tags_never_sent():
    messages = _first_turn("tab-h2")
    provider = _second_turn(messages, "tab-h2")
    sent = provider.sent_text()
    assert "merger" in sent and privacy.PRIV_TAG not in sent


def test_source_set_to_never_drops_its_history_but_not_other_sources():
    messages = _first_turn("tab-h3")
    privacy.PrivacyStore().set_consent("mail", "deny")
    provider = _second_turn(messages, "tab-h3")
    assert "merger" not in provider.sent_text()
    assert "Mail" in provider.sent_text() and "removed" in provider.sent_text()


def test_scrub_history_ignores_untagged_and_other_chats_text():
    msgs = [{"role": "user", "content": "my own words"}, {"role": "tool", "content": "public", "tool_call_id": "1"}]
    assert privacy.scrub_history(msgs, private=True) == 0
    assert msgs[0]["content"] == "my own words"
