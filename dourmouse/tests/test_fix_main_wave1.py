"""Wave 1 integration fixes by the main thread (finding #173)."""
from __future__ import annotations

import pytest

from dourmouse.dispatch import _argument_gate
from dourmouse.general_roster import build_general_registry


def _spec(name):
    registry = build_general_registry()
    subs = [registry.get_subagent(n) for n in registry.subagent_names]
    return next(t for sub in subs if sub is not None for t in sub.tools if t.name == name)


@pytest.mark.parametrize("key", ["Space", " ", "Control+Space", "spacebar", "Enter"])
def test_space_and_enter_presses_ask_first(key):
    decision = _argument_gate(_spec("browser_press"), {"key": key}, "orchestrator")
    assert decision is not None and decision[0] == "confirm"


def test_other_keys_do_not_ask():
    assert _argument_gate(_spec("browser_press"), {"key": "Tab"}, "orchestrator") is None


def test_browser_click_is_routed_through_the_click_gate(monkeypatch):
    from dourmouse import browser_agent

    seen = {}

    def fake(arguments):
        seen["args"] = arguments
        return ("confirm", "Click Send on example.com?")

    monkeypatch.setattr(browser_agent, "click_gate", fake)
    decision = _argument_gate(_spec("browser_click"), {"target": "e12"}, "orchestrator")
    assert decision == ("confirm", "Click Send on example.com?")
    assert seen["args"]["target"] == "e12"


def test_a_plain_click_passes_the_gate(monkeypatch):
    from dourmouse import browser_agent

    monkeypatch.setattr(browser_agent, "click_gate", lambda a: None)
    assert _argument_gate(_spec("browser_click"), {"target": "e3"}, "orchestrator") is None


def test_quarantine_refuses_system_paths_in_any_case(tmp_path):
    from pathlib import Path

    from dourmouse.security.response import ResponseRefused, _check_quarantinable

    for spelled in ("/System/Library/CoreServices", "/SYSTEM/Library/CoreServices", "/system/library"):
        p = Path(spelled)
        if not p.exists():
            continue  # a case-sensitive volume has no such alias; the real spelling is covered below
        with pytest.raises(ResponseRefused):
            _check_quarantinable(p)
    with pytest.raises(ResponseRefused):
        _check_quarantinable(Path("/System/Library"))


def test_long_confirmation_required_text_is_not_replaced_by_the_privacy_gate():
    """Finding #179: the dispatcher's own long CONFIRMATION REQUIRED message must reach the model."""
    from dourmouse import privacy

    text = "CONFIRMATION REQUIRED: run this privileged command: " + "x" * 1500 + " NOT executed."
    turn = privacy.TurnPrivacy(chat="t", private=False, placeholders=privacy.PlaceholderMap())
    out = privacy.gate_tool_result("run_privileged_command", {"command": "touch /tmp/a"}, text, turn=turn, own_text=True)
    assert out == text


def test_long_private_data_starting_with_error_is_still_gated():
    from dourmouse import privacy

    text = "ERROR " + "private data " * 200
    turn = privacy.TurnPrivacy(chat="t", private=True, placeholders=privacy.PlaceholderMap())
    out = privacy.gate_tool_result("run_privileged_command", {"command": "cat ~/Documents/x"}, text, turn=turn)
    assert out != text


def test_live_feeds_error_text_hides_secret_query_values():
    """Finding #181: URL errors keep the host and path but not api keys."""
    from dourmouse.live_feeds import _safe_url

    out = _safe_url("https://api.example.com/v1/x?symbol=AAPL&api_key=SECRET123&token=abc#f")
    assert "SECRET123" not in out and "abc" not in out and "symbol=AAPL" in out


def test_webui_broadcast_uses_the_bus_constant():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "dourmouse" / "webui.py").read_text()
    assert 'bus.post(sender, "BROADCAST"' not in src



def test_claude_deny_list_has_no_name_the_cli_rejects():
    """Finding #181: the CLI exits 1 on a deny rule that matches no known tool; these five did."""
    from dourmouse import code_backends

    names = set(code_backends._DISALLOWED_NATIVE_TOOLS.split(","))
    assert not names & {"MultiEdit", "NotebookRead", "LS", "SlashCommand", "REPL"}
    assert {"Bash", "Write", "Edit", "Read", "Glob", "Grep"} <= names  # the file readers stay denied


def test_front_mode_falls_back_when_the_claude_cli_is_signed_out(monkeypatch):
    """Finding #183: a signed-out Claude CLI must not make every HOME turn fail."""
    from dourmouse import code_backends, config, dispatch

    monkeypatch.delenv("DOURMOUSE_ORCHESTRATOR_MODE", raising=False)
    monkeypatch.setattr(config, "claude_front_mode_enabled", lambda: True)
    monkeypatch.setattr(code_backends, "claude_cli_signed_in", lambda **k: False)
    assert dispatch._orchestrator_backend_mode() == ""
    monkeypatch.setattr(code_backends, "claude_cli_signed_in", lambda **k: True)
    assert dispatch._orchestrator_backend_mode() == "split"
    monkeypatch.setattr(code_backends, "claude_cli_signed_in", lambda **k: None)  # unknown: keep the default
    assert dispatch._orchestrator_backend_mode() == "split"


def test_claude_cli_signed_in_reads_auth_status(monkeypatch):
    import subprocess

    from dourmouse import code_backends, general_roster

    monkeypatch.setattr(general_roster, "_find_claude_cli", lambda: "/fake/claude")
    code_backends._SIGNIN_CACHE["value"] = None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout='{"loggedIn": false}', stderr=""))
    from dourmouse.tests.conftest import REAL_CLAUDE_CLI_SIGNED_IN as real

    assert real(ttl=0) is False
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="garbage", stderr=""))
    assert real(ttl=0) is None
    code_backends._SIGNIN_CACHE["value"] = None


def test_a_pane_that_opens_over_another_screen_brings_browser_forward():
    """Finding #183: boot.js follows the pane state and goes to BROWSER when the pane opens elsewhere."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "boot.js").read_text()
    block = src[src.index("host.onPaneState((state) =>"):][:300]
    assert "state.open" in block and "router.go('browser')" in block and "router.current() !== 'BROWSER'" in block


def test_new_research_question_goes_to_the_pipeline_agent():
    """Finding #183 (owner test): NEW QUESTION was pinned to research_info (web search only), so the
    questions, claims and graph panels never filled. It must reach the agent that owns research_plan."""
    import pathlib

    from dourmouse.general_roster import build_general_registry

    src = (pathlib.Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "research" / "index.js").read_text()
    assert "focusAgent: 'evidence_pipeline'" in src and "focusAgent: 'research_info'" not in src
    registry = build_general_registry()
    pipeline = registry.get_subagent("evidence_pipeline")
    assert pipeline is not None and "research_plan" in {t.name for t in pipeline.tools}


def test_md_drops_model_citation_markers():
    """Finding #183: gpt-oss writes markers such as 【browser_open†page=1†L5】 into replies; they point at nothing."""
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node not available")
    root = pathlib.Path(__file__).resolve().parents[2]
    script = ("import {md} from './ui/assets/os/kit/md.js';"
              "const out=md('Python 3.15.0 is out 【browser_open†page=1†L5-L9】. Done.');"
              "console.log(JSON.stringify(out));")
    out = subprocess.run([node, "--input-type=module", "-e", script], cwd=root, capture_output=True, text=True, timeout=30, check=False)
    assert "browser_open" not in out.stdout and "Python 3.15.0 is out" in out.stdout, out.stderr[:300]
