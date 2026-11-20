"""T5 step 2 (finding 215): agent tools against the signed-in Dourmouse Chrome. Gate decisions per tool and engine,
refusal of the sign-in tools, engine selection, Google Docs addresses, and a live run against a local test page on a
temporary profile (skipped when Chrome is not installed)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dourmouse import browser_agent as ba
from dourmouse import chrome_profile as cp
from dourmouse import dispatch
from dourmouse.general_roster import build_general_registry

needs_chrome = pytest.mark.skipif(cp.chrome_binary() is None, reason="Google Chrome is not installed")


def _spec(name):
    return next(t for t in build_general_registry().get_subagent("browser").tools if t.name == name)


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(ba, "_SIGNED_IN", True)
    monkeypatch.setattr(ba, "_PAGE", type("P", (), {"url": "https://mail.google.com/mail/u/0/#inbox"})())
    yield


def _gate(name, args):
    return dispatch._argument_gate(_spec(name), args, "browser")


@pytest.mark.parametrize("name,args", [
    ("browser_fill", {"target": "To", "value": "friend@example.org"}),
    ("browser_fill_form", {"fields": []}),
    ("browser_type", {"target": "e5", "text": "hello"}),
    ("browser_select", {"target": "e7", "value": "Work"}),
    ("browser_press", {"key": "Tab"}),
])
def test_every_acting_tool_asks_on_the_signed_in_chrome(signed_in, name, args):
    decision = _gate(name, args)
    assert decision is not None and decision[0] == "confirm"
    assert "signed-in Dourmouse Chrome" in decision[1] and "mail.google.com" in decision[1]


def test_the_prompt_names_the_field_and_the_text(signed_in):
    prompt = _gate("browser_fill", {"target": "To", "value": "friend@example.org"})[1]
    assert "'To'" in prompt and "friend@example.org" in prompt


@pytest.mark.parametrize("name,args", [
    ("browser_fill", {"target": "To", "value": "x"}), ("browser_type", {"target": "e5", "text": "hi"}),
    ("browser_select", {"target": "e7", "value": "Work"}), ("browser_press", {"key": "Tab"}),
])
def test_nothing_extra_is_asked_off_the_signed_in_chrome(name, args):
    assert ba.signed_in_state()["active"] is False
    assert _gate(name, args) is None


@pytest.mark.parametrize("name", ["browser_snapshot", "browser_extract", "browser_screenshot", "browser_wait",
                                  "browser_keep_page", "browser_back"])
def test_reading_never_asks_even_on_the_signed_in_chrome(signed_in, name):
    assert _gate(name, {"target": "e1"}) is None


@pytest.mark.parametrize("name", ["browser_signin", "browser_creds_store", "browser_creds_list", "browser_creds_forget"])
def test_sign_in_and_vault_tools_are_refused_there(signed_in, name):
    decision = _gate(name, {"site": "example.org", "username": "u", "password": "p"})
    assert decision is not None and decision[0] == "refuse" and "signs in by hand" in decision[1]
    assert getattr(ba, name)({"site": "example.org", "username": "u", "password": "p"}).startswith("REFUSED")


def test_the_vault_tools_are_not_refused_off_the_signed_in_chrome():
    assert _gate("browser_creds_list", {}) is None
    assert not ba.browser_creds_list({}).startswith("REFUSED: the tools are acting in the signed-in")


def test_a_submit_like_click_still_uses_its_own_gate_first(signed_in, monkeypatch):
    monkeypatch.setattr(ba, "click_gate", lambda a: ("confirm", "Click Send?"))
    assert _gate("browser_click", {"target": "e9"}) == ("confirm", "Click Send?")


def test_a_plain_click_asks_on_the_signed_in_chrome(signed_in, monkeypatch):
    monkeypatch.setattr(ba, "click_gate", lambda a: None)
    decision = _gate("browser_click", {"target": "e9"})
    assert decision is not None and decision[0] == "confirm" and "click" in decision[1]


def test_engine_selection_forgets_the_signed_in_page_and_nothing_else(monkeypatch):
    monkeypatch.setattr(ba, "_SIGNED_IN", False)
    page = object()
    monkeypatch.setattr(ba, "_PAGE", page)
    monkeypatch.setattr(ba, "_PAGE_MODE", "headless")
    ba._select_engine(False)
    assert ba._PAGE is page  # no change: the headless page is kept
    ba._select_engine(True)
    assert ba._SIGNED_IN is True and ba._PAGE is None and ba._PAGE_MODE is None
    monkeypatch.setattr(ba, "_PAGE", page)
    monkeypatch.setattr(ba, "_PAGE_MODE", "signed_in")
    ba._select_engine(False)
    assert ba._SIGNED_IN is False and ba._PAGE is None and ba._PAGE_MODE is None


@pytest.mark.parametrize("url,expected", [
    ("https://docs.google.com/document/d/1AbC_d-9/edit?tab=t.0", "https://docs.google.com/document/d/1AbC_d-9/mobilebasic"),
    ("https://docs.google.com/document/u/1/d/1AbC_d-9/edit", "https://docs.google.com/document/d/1AbC_d-9/mobilebasic"),
    ("https://docs.google.com/spreadsheets/d/1AbC/edit", "https://docs.google.com/spreadsheets/d/1AbC/edit"),
    ("https://example.org/document/d/1AbC/edit", "https://example.org/document/d/1AbC/edit"),
    ("https://mail.google.com/mail/u/0/", "https://mail.google.com/mail/u/0/"),
])
def test_google_docs_address_handling(url, expected):
    assert ba.google_docs_read_address(url) == expected


def test_browser_open_reports_the_chrome_that_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv(cp.PROFILE_ENV, str(tmp_path / "none"))
    try:
        with pytest.raises(RuntimeError, match="NOT SIGNED IN") as caught:
            ba.browser_open({"url": "https://example.org/", "signed_in": True})
    finally:
        ba._select_engine(False)
    assert "dourmouse_chrome_open" in str(caught.value)


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"<title>Test inbox</title><h1>Inbox</h1><input id=to placeholder=To><button>Compose</button>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@needs_chrome
def test_live_signed_in_open_reads_a_page_and_leaves_the_chrome_running(tmp_path, monkeypatch):
    monkeypatch.setenv(cp.PROFILE_ENV, str(tmp_path / "Chrome Profile"))
    server = HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        info = cp.launch(headless=True)
        out = ba.browser_open({"url": f"http://127.0.0.1:{server.server_port}/", "signed_in": True})
        assert "TITLE: Test inbox" in out and "Compose" in out
        assert ba.signed_in_state()["active"] is True
        assert ba._PAGE_MODE == "signed_in"
        ba.close_browser()  # drops the connection only
        assert cp.lock_pid() == info["pid"]
        assert ba.signed_in_state()["active"] is False
        assert ba.browser_status()["dourmouse_chrome"]["running"] is True
    finally:
        ba.close_browser()
        cp.quit_chrome()
        server.shutdown()
