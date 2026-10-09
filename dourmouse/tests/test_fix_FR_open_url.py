"""FR fix P2-7: open_url scheme check and the argument gate."""

from __future__ import annotations

import pytest

from dourmouse import general_roster as gr
from dourmouse.dispatch import _url_gate

_BAD = [
    "http:127.0.0.1:8765", "//127.0.0.1:8765/", "localhost:8765/x", "file:///etc/hosts",
    "smb://nas/share", "vnc://127.0.0.1", "javascript:alert(1)", "http://", "x-apple.systempreferences:com.apple.preference",
]


@pytest.mark.parametrize("url", _BAD)
def test_the_gate_refuses_anything_that_is_not_a_plain_http_address(url):
    for tool in ("open_url", "browser_open", "open_browser_pane"):
        decision = _url_gate(tool, url, "orchestrator")
        assert decision is not None and decision[0] == "refuse", (tool, url)


@pytest.mark.parametrize("url", _BAD)
def test_the_tool_itself_never_opens_it(url, monkeypatch):
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: opened.append(a) or True)
    out = gr._open_url_tool({"url": url})
    assert out.startswith("REFUSED") and opened == []


def test_a_normal_address_still_opens(monkeypatch):
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: opened.append(a) or True)
    assert gr._open_url_tool({"url": "https://example.com/page?q=1"}).startswith("OPENED")
    assert opened and _url_gate("open_url", "https://example.com/page?q=1", "orchestrator") is None


def test_own_port_is_still_refused_in_the_plain_spelling():
    decision = _url_gate("open_url", "http://127.0.0.1:8765", "orchestrator")
    assert decision is not None and decision[0] == "refuse"
