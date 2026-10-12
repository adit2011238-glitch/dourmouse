"""PRIV-3, A-13, P5-31, P2-21: the "private agents stay local" promise is removed, not repaired.

Cloud models only (owner decision 2026-10-09). force_local and _LOCAL_ONLY_AGENTS routing are gone;
the privacy gate applies whatever the provider; (g) prompts, tool descriptions and UI no longer
claim private content stays local for model use, while the storage claims stay.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
from typing import Any

import dourmouse
from dourmouse import model_delegation as md
from dourmouse import privacy
from dourmouse.config import OllamaConfig, load_ollama_config

ROOT = pathlib.Path(inspect.getfile(dourmouse)).parent
REPO = ROOT.parent


# --------------------------------------------------------------------------- #
# A-13 / PRIV-3: no local routing left
# --------------------------------------------------------------------------- #

def test_a13_load_ollama_config_has_no_force_local():
    assert "force_local" not in inspect.signature(load_ollama_config).parameters
    assert not hasattr(OllamaConfig(), "skip_persisted_orchestrator_choice")


def test_priv3_no_local_only_routing_names_remain_in_code():
    hits = []
    for py in ROOT.rglob("*.py"):
        if "tests" in py.parts:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        for word in ("_LOCAL_ONLY_AGENTS", "force_local", "_config_for_agent_model"):
            if word in src:
                hits.append(f"{py.relative_to(ROOT)}: {word}")
    assert hits == []


def test_priv3_sensitive_agents_are_the_work_list_set():
    expected = {
        "mail", "comms", "messenger", "docs", "memory", "companion", "admin_ops", "system", "tasks",
        "scheduling", "compute", "browser", "panel_control", "freebuff", "design_3d", "apps",
        "app_driver", "markets", "forex", "mt5", "t212",
    }
    assert expected == privacy.SENSITIVE_AGENTS


def test_a13_a_sensitive_agent_runs_on_the_configured_cloud_backend(monkeypatch):
    """No silent local swap: a mail turn uses the same configured client as
    any other agent, and the gate (not the routing) protects its content."""
    from dourmouse import dispatch

    monkeypatch.setenv(dispatch._CLAUDE_ORCHESTRATOR_ENV, "off")
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    cfg = load_ollama_config()
    client = dispatch._build_client(cfg, forced_agent="mail")
    assert isinstance(client, dispatch.OllamaNativeClient) and client._root == "https://ollama.com"


# --------------------------------------------------------------------------- #
# P5-31: a model-supplied "model" cannot move a sensitive agent's work
# --------------------------------------------------------------------------- #

def test_p531_task_model_cannot_send_a_sensitive_agent_to_tool_less_gemini(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(md, "gemini_available", lambda: True)
    monkeypatch.setattr(md, "_run_gemini", lambda t, timeout: ran.append("gemini") or md.DelegationResult(t, True, "g"))
    monkeypatch.setattr(md, "_run_roster", lambda t, timeout: ran.append("roster") or md.DelegationResult(t, True, "r"))
    md.delegate([md.DelegationTask(prompt="<inbox> summarise", agent="mail", model="gemini")], timeout=5)
    md.delegate([md.DelegationTask(prompt="news today", agent="news", model="gemini")], timeout=5)
    md.delegate([md.DelegationTask(prompt="anything", agent=None, model="gemini")], timeout=5)
    # Once this request has read a sensitive source, no task hops to Gemini:
    # the model-written prompt may quote what it read.
    with privacy.turn_scope("tab-x") as turn:
        turn.sources.add("mail")
        md.delegate([md.DelegationTask(prompt="news today", agent="news", model="gemini")], timeout=5)
        md.delegate([md.DelegationTask(prompt="news today", agent="news")], timeout=5)
    assert ran == ["roster", "gemini", "gemini", "roster", "roster"]


def test_p531_routing_policy(monkeypatch):
    monkeypatch.setattr(md, "gemini_available", lambda: True)
    for agent in sorted(privacy.SENSITIVE_AGENTS):
        assert md.route_for(agent) == md.ROSTER, agent
    assert md.route_for("news") == md.GEMINI
    assert md.route_for("some_future_agent") == md.ROSTER
    assert md.route_for("news", allow_gemini=False) == md.ROSTER
    monkeypatch.setattr(md, "gemini_available", lambda: False)
    assert md.route_for("news") == md.ROSTER


def test_p531_gemini_prompt_is_swapped_and_recorded_and_the_answer_restored(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_call(prompt: str, **kw: Any) -> str:
        seen["prompt"] = prompt
        return "Write to [EMAIL_1] today."

    from dourmouse import gemini_backend

    monkeypatch.setattr(gemini_backend, "call_gemini", fake_call)
    with privacy.chat_scope("tab-g"):
        result = md._run_gemini(md.DelegationTask(prompt="contact bob@example.org about it", agent="news"), 5)
    assert "bob@example.org" not in seen["prompt"] and "[EMAIL_1]" in seen["prompt"]
    assert result.ok and result.text == "Write to bob@example.org today."


def test_p531_gemini_call_writes_an_egress_row(monkeypatch):
    from dourmouse import gemini_backend

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTESTKEY0123456789abcdef")
    frames = [b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]},"finishReason":"STOP"}]}\n']

    def transport(url: str, *, data: bytes, headers: dict[str, str], timeout: float):
        return iter(line.decode() for line in frames)

    gemini_backend.call_gemini("a private question", transport=transport)
    row = privacy.read_egress(1)[0]
    assert row["backend"] == "gemini"
    assert "private question" not in json.dumps(row)


# --------------------------------------------------------------------------- #
# P2-21: a delegated run shares the turn's gate
# --------------------------------------------------------------------------- #

def test_p221_a_nested_run_inherits_private_mode_and_the_map():
    from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec, run_dispatch_messages

    seen_tools: list[list[str]] = []

    class _Client:
        def __init__(self) -> None:
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kw: Any) -> Any:
            seen_tools.append([t["function"]["name"] for t in kw.get("tools") or []])
            msg = type("M", (), {"content": "ok", "tool_calls": None})()
            return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()

    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="mail", domain="Mail", description="mail", tools=[
        ToolSpec(name="gmail_read", description="r", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "x"),
        ToolSpec(name="gmail_send", description="s", parameters={"type": "object", "properties": {}},
                 handler=lambda a: "x")]))
    privacy.PrivacyStore().set_chat_private("tab-parent", True)
    with privacy.turn_scope("tab-parent") as parent:
        run_dispatch_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "inbox"}],
                              reg, client=_Client(), forced_agent="mail", depth=1, max_turns=1)
        assert privacy.current_turn() is parent
    assert seen_tools == [["gmail_send"]]


# --------------------------------------------------------------------------- #
# (g) the false local claims are gone; storage claims stay
# --------------------------------------------------------------------------- #

_MODEL_LOCAL_CLAIMS = re.compile(
    r"(?i)(stays? local\b|stay on (?:this|the) (?:mac|machine)|stays on (?:this|the) (?:mac|machine)"
    r"|never leaves? (?:this|the|your) (?:mac|machine|laptop)|pinned (?:to the )?local|local[- ]only agent"
    r"|private agents stay|runs? on the local model|routes? to ollama, which runs on this machine"
    r"|inference local|local model, keyless|local ollama llm backend"
    r"|locally (?:hosted|available) (?:ollama )?models|privacy[- ]pinned)"
)

#: Lines that stay because they are true: about STORAGE on this Mac, network
#: traffic that really stays on loopback, or data a refusal withholds.
_ALLOWED = {
    # security privacy mode withholds evidence from the model: it really stays
    "dourmouse/security/privacy.py", "dourmouse/security/tools.py",
    # a loopback address: network traffic, not model use
    "dourmouse/browser_agent.py",
    # saved passwords are encrypted on this Mac and no AI tool can read them
    "ui/assets/os/screens/browser/privacy-model.js", "ui/assets/os/screens/browser/index.js",
    # the setup page's key file is stored on this machine
    "ui/setup.html",
}


def _claim_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if _MODEL_LOCAL_CLAIMS.search(line)]


def test_g_no_prompt_description_or_comment_claims_model_content_stays_local():
    offenders = []
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(REPO).as_posix()
        if "tests" in py.parts or rel in _ALLOWED:
            continue
        for line in _claim_lines(py.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{rel}: {line[:140]}")
    assert offenders == []


def test_g_no_ui_text_claims_model_content_stays_local():
    offenders = []
    for path in list((REPO / "ui").rglob("*.js")) + list((REPO / "ui").rglob("*.html")):
        rel = path.relative_to(REPO).as_posix()
        if rel in _ALLOWED:
            continue
        for line in _claim_lines(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{rel}: {line[:140]}")
    assert offenders == []


def test_g_storage_claims_stay_true():
    """The vault and the egress ledger are files on this Mac: the ledger is
    written under the workspace and never holds content."""
    privacy.record_egress(backend="gemini", model="m", text="secret words")
    path = privacy.egress_path()
    assert path.exists() and "secret words" not in path.read_text(encoding="utf-8")
    from dourmouse.config import workspace_dir

    assert path.resolve().is_relative_to(workspace_dir().resolve())


def test_g_the_privacy_screen_says_the_honest_limit():
    from dourmouse.os_api import privacy as route

    assert "runs in the cloud" in route._LIMITS and "names and other free text are not" in route._LIMITS
