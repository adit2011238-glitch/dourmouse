"""dispatch.py _SYSTEM_PROMPT — Rule 22: real computer access, not a
sandboxed demo.

2026-09-15, real live-reported bug: a weaker model (gpt-oss:20b via
Ollama Cloud) sometimes answered an ordinary request as if it had no
real access to the user's machine at all ("I can't access your
terminal/computer"), instead of just calling the real tool it actually
has (run_command already worked fine live when tried directly — the
failure was the model's own hedging, not a real technical block). This
rule exists to make that hedging incorrect by the prompt's own explicit
statement, for every backend that reads it.
"""

from __future__ import annotations

from dourmouse.dispatch import _SYSTEM_PROMPT


class TestRealComputerAccessRule:
    def test_states_real_access_explicitly(self):
        assert "REAL, WORKING ACCESS TO THIS COMPUTER" in _SYSTEM_PROMPT
        assert "not in a restricted demo" in _SYSTEM_PROMPT

    def test_names_the_real_tools_it_refers_to(self):
        for tool in ("run_command", "read_path", "write_path", "browser", "apps"):
            assert tool in _SYSTEM_PROMPT

    def test_names_the_real_failure_mode_it_closes(self):
        assert "I can't access your terminal" in _SYSTEM_PROMPT

    def test_still_requires_honest_reporting_of_a_real_refusal(self):
        """This rule must not contradict Rules 2/3 (report a real
        CONFIRMATION REQUIRED/NOT CONFIGURED/REFUSED honestly) -- it only
        forbids a GENERIC, untested claim that the capability itself
        doesn't exist, not honest reporting of one real, specific
        failure."""
        assert "REAL, SPECIFIC result" in _SYSTEM_PROMPT
        assert "generic, untested claim" in _SYSTEM_PROMPT
