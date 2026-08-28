"""Tests for dourmouse/voice_commands.py — the Vision workspace's bounded
voice/text command grammar (world-monitor-expansion, item 3).

Pure function, no server/socket/model involved — every case here is a
deterministic parse, exactly what Rule 2.8 asks for.
"""

from __future__ import annotations

from dourmouse.voice_commands import (
    VoiceCommand,
    available_commands,
    parse_voice_command,
    resolve_panel,
)


class TestEmailCommand:
    def test_saying_form(self):
        cmd = parse_voice_command("email sam saying I'm running ten minutes late")
        assert cmd is not None
        assert cmd.action == "email"
        assert cmd.args == {"person": "sam", "message": "I'm running ten minutes late"}

    def test_colon_form(self):
        cmd = parse_voice_command("email jordan: the report is ready")
        assert cmd is not None
        assert cmd.args == {"person": "jordan", "message": "the report is ready"}

    def test_that_form(self):
        cmd = parse_voice_command("email priya that the meeting moved to 3pm")
        assert cmd is not None
        assert cmd.args == {"person": "priya", "message": "the meeting moved to 3pm"}

    def test_case_insensitive(self):
        cmd = parse_voice_command("EMAIL Sam SAYING hello there")
        assert cmd is not None
        assert cmd.action == "email"
        assert cmd.args["person"] == "Sam"

    def test_missing_message_does_not_match(self):
        assert parse_voice_command("email sam") is None

    def test_email_address_as_person(self):
        cmd = parse_voice_command("email sam@example.com saying hi")
        assert cmd is not None
        assert cmd.args["person"] == "sam@example.com"


class TestOpenCloseCommand:
    def test_open_mail(self):
        cmd = parse_voice_command("open mail")
        assert cmd == VoiceCommand("open_panel", {"panel": "mail"}, "open mail")

    def test_open_with_the_and_panel_suffix(self):
        cmd = parse_voice_command("open the mail panel")
        assert cmd is not None
        assert cmd.action == "open_panel"
        assert cmd.args == {"panel": "mail"}

    def test_open_gmail_alias(self):
        cmd = parse_voice_command("open gmail")
        assert cmd is not None
        assert cmd.args["panel"] == "mail"

    def test_open_world_map(self):
        cmd = parse_voice_command("open world map")
        assert cmd is not None
        assert cmd.args["panel"] == "map"

    def test_open_companion_chat(self):
        cmd = parse_voice_command("open companion")
        assert cmd is not None
        assert cmd.args["panel"] == "chat"

    def test_close_research(self):
        cmd = parse_voice_command("close research")
        assert cmd == VoiceCommand("close_panel", {"panel": "research"}, "close research")

    def test_unrecognized_panel_returns_none(self):
        assert parse_voice_command("open the kitchen") is None

    def test_open_with_no_panel_returns_none(self):
        assert parse_voice_command("open") is None


class TestSearchCommand:
    def test_search_for(self):
        cmd = parse_voice_command("search for nvidia earnings")
        assert cmd is not None
        assert cmd.action == "search"
        assert cmd.args == {"query": "nvidia earnings"}

    def test_search_without_for(self):
        cmd = parse_voice_command("search weather in tokyo")
        assert cmd is not None
        assert cmd.args == {"query": "weather in tokyo"}

    def test_empty_query_returns_none(self):
        assert parse_voice_command("search for") is None
        assert parse_voice_command("search") is None


class TestUnrecognized:
    def test_empty_string_returns_none(self):
        assert parse_voice_command("") is None
        assert parse_voice_command("   ") is None

    def test_none_returns_none(self):
        assert parse_voice_command(None) is None

    def test_unmatched_free_text_returns_none(self):
        assert parse_voice_command("what's the weather like today") is None

    def test_never_raises_on_garbage(self):
        for garbage in ["\x00\x01", "a" * 5000, "😀😀😀 email", "open " + "x" * 500]:
            parse_voice_command(garbage)  # must not raise


class TestResolvePanel:
    def test_known_aliases(self):
        assert resolve_panel("mail") == "mail"
        assert resolve_panel("Gmail") == "mail"
        assert resolve_panel("world") == "map"
        assert resolve_panel("world pulse") == "map"
        assert resolve_panel("companion") == "chat"
        assert resolve_panel("research panel") == "research"

    def test_unknown_returns_none(self):
        assert resolve_panel("kitchen") is None
        assert resolve_panel("") is None


class TestAvailableCommands:
    def test_returns_four_real_commands(self):
        cmds = available_commands()
        assert len(cmds) == 4
        for c in cmds:
            assert "pattern" in c and "example" in c
            assert c["pattern"].strip()
            assert c["example"].strip()

    def test_every_documented_example_actually_parses(self):
        # The single most important guard in this file: the help text must
        # never describe a command the parser doesn't actually recognize.
        for c in available_commands():
            example = c["example"].split("  (")[0].strip()
            assert parse_voice_command(example) is not None, (
                f"documented example does not parse: {example!r}"
            )
