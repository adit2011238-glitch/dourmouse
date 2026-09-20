"""Tests for dourmouse/sdk.py (Domain H, piece 6: programmable headless
interface). Reuses test_chat.py's own FakeClient/_registry fixtures --
same convention test_learn.py already established for cross-module reuse.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from dourmouse import hooks
from dourmouse.sdk import Dourmouse, main
from dourmouse.tests.test_chat import FakeClient, _FakeMessage, _FakeResponse, _registry


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class TestDourmouseFacade:
    def test_ask_returns_the_real_unmodified_report(self, tmp_path):
        client = FakeClient([_FakeResponse(_FakeMessage(content="the real answer"))])
        d = Dourmouse(_registry(), client=client, session_file=tmp_path / "sdk1.jsonl")
        report = d.ask("hello")
        assert report["final_text"] == "the real answer"
        assert "transcript" in report

    def test_resolves_its_own_registry_when_none_given(self, tmp_path):
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        d = Dourmouse(client=client, session_file=tmp_path / "sdk2.jsonl")
        assert d.registry is not None
        report = d.ask("hello")
        assert report["final_text"] == "ok"

    def test_close_fires_the_real_session_stop_hook(self, tmp_path):
        seen = []
        hooks.register_session_stop_hook(seen.append)
        client = FakeClient([])
        d = Dourmouse(_registry(), client=client, session_file=tmp_path / "sdk3.jsonl")
        assert seen == []
        d.close()
        assert seen == [d.session.session_file.stem]

    def test_context_manager_calls_close_on_exit(self, tmp_path):
        seen = []
        hooks.register_session_stop_hook(seen.append)
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        with Dourmouse(_registry(), client=client, session_file=tmp_path / "sdk4.jsonl") as d:
            d.ask("hello")
            assert seen == []
        assert seen == [d.session.session_file.stem]

    def test_forced_agent_kwarg_passes_through_to_ask(self, tmp_path, monkeypatch):
        captured = {}
        import dourmouse.chat as chat_mod

        real_run = chat_mod.run_dispatch_messages

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(chat_mod, "run_dispatch_messages", spy)
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        d = Dourmouse(_registry(), client=client, session_file=tmp_path / "sdk5.jsonl")
        d.ask("hello", forced_agent="echo_agent")
        assert captured.get("forced_agent") == "echo_agent"


class _StubDourmouse:
    """Stands in for the real Dourmouse facade in CLI tests -- same real
    ChatSession underneath (a FakeClient, no network), just constructed
    ahead of time so the test controls the exact canned response."""

    def __init__(self, session):
        self.session = session

    def ask(self, prompt, **kwargs):
        return self.session.ask(prompt, **kwargs)

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class TestSdkCli:
    def test_main_prints_final_text_plainly(self, tmp_path, monkeypatch, capsys):
        from dourmouse.chat import ChatSession

        client = FakeClient([_FakeResponse(_FakeMessage(content="cli answer"))])
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "cli1.jsonl")
        monkeypatch.setattr("dourmouse.sdk.Dourmouse", lambda *a, **k: _StubDourmouse(session))
        rc = main(["hello there"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "cli answer"

    def test_main_json_flag_prints_valid_json_with_final_text(self, tmp_path, monkeypatch, capsys):
        from dourmouse.chat import ChatSession

        client = FakeClient([_FakeResponse(_FakeMessage(content="json answer"))])
        session = ChatSession(_registry(), client=client, session_file=tmp_path / "cli2.jsonl")
        monkeypatch.setattr("dourmouse.sdk.Dourmouse", lambda *a, **k: _StubDourmouse(session))
        rc = main(["hello there", "--json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["final_text"] == "json answer"

    def test_module_is_actually_runnable_as_python_dash_m(self):
        # Real subprocess smoke test: `python -m dourmouse.sdk --help` must
        # exit 0 and describe itself, proving the module is genuinely
        # invocable this way, not just importable.
        result = subprocess.run(
            [sys.executable, "-m", "dourmouse.sdk", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "Headless" in result.stdout
