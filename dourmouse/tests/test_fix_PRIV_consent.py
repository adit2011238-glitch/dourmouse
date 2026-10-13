"""PRIV stage 3: consent cards and Private mode, owner-only routes (PRIV-1, PRIV-5 backend).

(f) no model-reachable route can change consent or Private mode: the routes are owner-only, and no
tool in the real registry writes the privacy store.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from typing import Any

import pytest

from dourmouse import privacy, request_guard, webui
from dourmouse.general_roster import build_general_registry

SECRET = "p" * 20 + "RIV_secret_0123456789-_abcd"

ROUTES = {
    "/api/os/settings/privacy/consent": {"source": "mail", "decision": "allow"},
    "/api/os/settings/privacy/private_mode": {"enabled": True},
    "/api/os/settings/privacy/chat": {"tab_id": "tab-1", "enabled": True},
    "/api/os/settings/privacy/provider": {"id": "gemini", "verified": True},
}


@pytest.fixture
def make_server(monkeypatch, tmp_path):
    started: list = []

    def start(**kwargs: Any):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        srv = webui.run_server(build_general_registry(), port=0, client=None, config=None, **kwargs)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv

    yield start
    for srv, thread in started:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(srv, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw or b"{}")


def _same_origin(srv) -> dict[str, str]:
    port = srv.server_address[1]
    return {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}


def test_f_every_privacy_write_is_an_owner_route():
    for path in ROUTES:
        assert path in request_guard.OWNER_ONLY_POST
        assert request_guard.is_owner_route("POST", path)
    assert not request_guard.is_owner_route("GET", "/api/os/privacy/egress")


def test_f_a_page_without_the_owner_secret_changes_nothing(make_server):
    srv = make_server(owner_secret=SECRET)
    for path, body in ROUTES.items():
        status, data = _request(srv, "POST", path, body, _same_origin(srv))
        assert status == 403 and data["error"] == "owner only", path
    store = privacy.PrivacyStore()
    assert store.consent("mail") is None
    assert not store.private_mode() and not store.chat_private("tab-1")


def test_f_the_owner_window_changes_consent_and_private_mode(make_server):
    srv = make_server(owner_secret=SECRET)
    owner = {**_same_origin(srv), "Cookie": f"dourmouse_owner={SECRET}"}
    for path, body in ROUTES.items():
        status, data = _request(srv, "POST", path, body, owner)
        assert status == 200 and data["ok"] is True, (path, data)
    status, data = _request(srv, "GET", "/api/os/settings/privacy?tab_id=tab-1")
    assert status == 200
    assert {s["id"]: s["decision"] for s in data["sources"]}["mail"] == "allow"
    assert data["private_mode"] is True and data["chat"]["private"] is True
    assert {p["id"]: p["verified"] for p in data["providers"]}["gemini"] is True
    status, data = _request(srv, "POST", "/api/os/settings/privacy/consent",
                            {"source": "mail", "decision": "sometimes"}, owner)
    assert status == 400


def test_f_no_tool_writes_the_privacy_store(monkeypatch):
    """Run every tool handler that mentions privacy, consent or private mode
    would be guesswork; instead: no tool's name or description offers to
    change the consent or Private mode of this gate, and the store's writer
    is only called from os_api.privacy (checked in source)."""
    import inspect
    import pathlib

    import dourmouse

    registry = build_general_registry()
    offer = re.compile(r"(?i)(turn|set|change|grant|give|allow|switch)\w*\W+(?:\w+\W+){0,4}(consent|private mode)")
    for sub in registry.all_subagents():
        for tool in sub.tools:
            assert "consent" not in tool.name and "private_mode" not in tool.name, tool.name
            assert not offer.search(tool.description), tool.name
    root = pathlib.Path(inspect.getfile(dourmouse)).parent
    writers = []
    for py in root.rglob("*.py"):
        if "tests" in py.parts:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        if any(w in src for w in ("set_consent(", "set_private_mode(", "set_chat_private(", "set_provider_verified(")):
            writers.append(py.relative_to(root).as_posix())
    # privacy.py itself (the store and the consent card's remembered answer)
    # and the owner-only routes. security/privacy.py has its own, separate
    # set_privacy_mode for the security agent and is not this store.
    assert sorted(w for w in writers if w != "security/privacy.py") == ["os_api/privacy.py", "privacy.py"]


def test_egress_route_returns_metadata_rows(make_server):
    srv = make_server()
    privacy.record_egress(backend="gemini", model="g", text="a private sentence")
    status, data = _request(srv, "GET", "/api/os/privacy/egress?limit=5")
    assert status == 200 and data["summary"]["requests"] == 1
    assert "private sentence" not in json.dumps(data)


# --------------------------------------------------------------------------- #
# The web gate's consent card
# --------------------------------------------------------------------------- #

def _gate(events: list[dict[str, Any]]) -> webui.WebConfirmationGate:
    return webui.WebConfirmationGate(events.append)


def _answer_soon(gate: webui.WebConfirmationGate, events: list[dict[str, Any]], approved: bool) -> threading.Thread:
    def run() -> None:
        for _ in range(200):
            if events:
                gate.resolve(events[-1]["id"], approved)
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=run)
    t.start()
    return t


def test_consent_card_is_never_answered_by_auto_approve(monkeypatch):
    monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: True)
    events: list[dict[str, Any]] = []
    gate = _gate(events)
    assert gate("send the mail?") is True and events == []  # an action card: auto approved
    t = _answer_soon(gate, events, False)
    assert gate.ask_owner("Send your Mail to Ollama Cloud?") is False
    t.join()
    assert events[0]["kind"] == "privacy_consent"


def test_consent_card_nobody_saw_is_not_a_no(monkeypatch):
    gate = webui.WebConfirmationGate(lambda e: None)
    gate.set_emit(webui._decline_confirmations(gate))
    assert gate.ask_owner("Send your Mail?") is None
    assert gate("an action?") is False  # an action card still declines at once


def test_consent_card_timeout_is_not_a_no(monkeypatch):
    monkeypatch.setattr(webui, "_CONFIRM_TIMEOUT_SECONDS", 0.05)
    gate = _gate([])
    assert gate.ask_owner("Send your Mail?") is None


def test_asker_stores_only_owner_answers():
    events: list[dict[str, Any]] = []
    gate = _gate(events)
    ask = privacy.asker_for(gate)
    t = _answer_soon(gate, events, True)
    assert ask("q") == (True, True)
    t.join()
    # FIX-P R5-4: a plain callable (auto approve, a stub) is never asked; the old assertion pinned that bug.
    assert privacy.asker_for(lambda p: True) is None
    assert privacy.asker_for(None) is None


def test_chat_scope_reaches_the_turn():
    privacy.PrivacyStore().set_chat_private("tab-9", True)
    privacy.enter_chat("tab-9")
    try:
        with privacy.turn_scope() as turn:
            assert turn.chat == "tab-9" and turn.private is True
    finally:
        privacy.leave_chat()
    assert privacy.current_chat() == ""
