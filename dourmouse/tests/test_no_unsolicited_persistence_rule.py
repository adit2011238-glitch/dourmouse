"""dispatch.py _SYSTEM_PROMPT — Rule 12: no unsolicited write_note/remember.

Live-reproduced bug: asked a plain "write me a N word essay about X" on
HOME (no persistence requested), gpt-oss:20b answered correctly, THEN
unprompted called write_note to save the essay to the Obsidian vault --
every single time, for three different essays in a row. Since
OBSIDIAN_VAULT_PATH was never configured on this machine the call was a
guaranteed NOT CONFIGURED failure (google_services-style honest error, no
fabrication), but it's also a REQUIRES_CONFIRMATION tool -- so each stray
call popped an unwanted approval box and, worse, sat there blocking
session_lock exactly like the STOP-button bug already fixed
(test_console_stop_declines_confirmation.py) until someone manually
declined it.

Nothing in the roster prompt told the model NOT to do this -- it decided
on its own that a finished long-form answer was "worth saving". Rule 12
closes that gap explicitly.
"""

from __future__ import annotations

from dourmouse.dispatch import _SYSTEM_PROMPT


class TestNoUnsolicitedPersistenceRule:
    def test_rule_present(self):
        assert "write_note" in _SYSTEM_PROMPT
        assert "OWN answer" in _SYSTEM_PROMPT

    def test_rule_names_both_persistence_tools(self):
        # write_note (vault) and remember (long-term memory store) are the
        # two tools that can silently persist a just-generated answer.
        assert "remember" in _SYSTEM_PROMPT

    def test_rule_carves_out_explicit_user_requests(self):
        assert "explicitly asked" in _SYSTEM_PROMPT
