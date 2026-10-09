"""FR fixes P2-23 .. P2-29: the bespoke agent prompts must name real tools and
describe them truthfully."""

from __future__ import annotations

import re

import pytest

from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
from dourmouse.general_roster import build_general_registry

# Bracketed words in the prompts that are placeholders in an example sentence,
# not tool names.
_PLACEHOLDERS = {"event", "date", "time", "hidden"}


@pytest.fixture(scope="module")
def registry():
    return build_general_registry()


def test_every_bracketed_token_is_a_real_tool_or_agent(registry):
    known = set(registry.tool_names) | set(registry.subagent_names)
    bad = {}
    for agent, text in AGENT_SYSTEM_PROMPTS.items():
        for tok in re.findall(r"\[([A-Za-z0-9_\-]+)\]", text):
            if tok not in known and tok not in _PLACEHOLDERS:
                bad.setdefault(tok, set()).add(agent)
    assert not bad, bad


def test_mail_prompt_never_uses_email_own_send_as_a_checker():
    text = AGENT_SYSTEM_PROMPTS["mail"]
    assert "checking whether a specified email address belongs" not in text
    rule = text[text.index("12.If determining whether"):][:300]
    assert "[email_identity_status]" in rule and "sends mail" in rule


def test_research_prompt_does_not_send_the_agent_to_open_url_to_inspect():
    text = AGENT_SYSTEM_PROMPTS["research_info"]
    assert "open and inspect retrieved URLs" not in text
    assert "[fetch_url]" in text[text.index("[open_url]"):][:500]


def test_music_prompt_matches_the_spotify_tools():
    text = AGENT_SYSTEM_PROMPTS["music"]
    assert "generating or retrieving Spotify links" not in text
    assert "If the user asks for a Spotify link, use [spotify_link]." not in text
    assert "open.spotify.com/track/" in text
    assert "seeking" not in text and "Seek" not in text


def test_comms_prompt_does_not_promise_sending(registry):
    text = AGENT_SYSTEM_PROMPTS["comms"]
    assert "send an already-prepared draft" not in text
    assert "NOT CONFIGURED" in text[text.index("[send_draft] →"):][:300]
    assert "[mail]" in text


def test_docs_prompt_promises_no_drive_delete_and_lists_its_tools(registry):
    text = AGENT_SYSTEM_PROMPTS["docs"]
    usage = text[text.index("TOOL USAGE:"):text.index("DECISION RULES:")]
    owned = {t.name for t in registry.get_subagent("docs").tools} - {"query_desktop_vault", "query_shared_memory"}
    for name in owned:
        assert f"[{name}]" in usage, name
    assert "require confirmation before deletion" not in text
    assert "no tool can delete or" in text


def test_prompts_do_not_contradict_the_gates():
    sched = AGENT_SYSTEM_PROMPTS["scheduling"]
    assert "[create_calendar_event]" in sched[sched.index("TOOL USAGE:"):sched.index("DECISION RULES:")]
    assert "without an additional confirmation step" not in sched
    assert "does not require an\n      additional confirmation" not in sched
    browser = AGENT_SYSTEM_PROMPTS["browser"]
    assert "Forgetting stored credentials → no confirmation required" not in browser
    admin = AGENT_SYSTEM_PROMPTS["admin_ops"]
    assert "Call [delete_file] only after confirmation" not in admin
    assert "Do not call [delete_file] until the user has explicitly confirmed" not in admin


def test_stale_capability_claims_are_gone():
    system = AGENT_SYSTEM_PROMPTS["system"]
    assert "checking active network connections" not in system
    assert "does not report listening services" in system
    assert "local inference infrastructure" not in AGENT_SYSTEM_PROMPTS["worldmonitor"]
    assert "exactly two tools" not in AGENT_SYSTEM_PROMPTS["companion"]
    assert not AGENT_SYSTEM_PROMPTS["news"].rstrip().endswith("DOURMOUSE [markets] Agent")
