"""FIX-P R5-9 and R5-10: the Claude passthrough keeps placeholders in the session history and the recap, and
drops sensitive earlier answers once the chat is Private."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import privacy
from dourmouse.tests.test_webui import _registry_with_code_claude
from dourmouse.webui import run_server


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_registry_with_code_claude(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _chat(srv, prompt: str, tab: str = "") -> list[dict]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"prompt": prompt, "focus_agent": "code_claude", "screen": "CODE", "tab_id": tab}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
    while True:
        line = resp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            events.append(json.loads(line[6:]))
    conn.close()
    return events


def test_history_and_recap_keep_placeholders_but_the_owner_sees_real_values(server, monkeypatch):
    prompts: list[str] = []

    def fake_stream_claude(task, **kwargs):
        prompts.append(task)
        privacy.placeholder_map("default").placeholder("EMAIL", "alice@example.org")
        return "Write to alice@example.org, card 4111 1111 1111 1111."

    monkeypatch.setattr("dourmouse.code_backends.stream_claude", fake_stream_claude)
    events = _chat(server, "who do I write to")
    assert next(e for e in events if e["type"] == "done")["final_text"].startswith("Write to alice@example.org")
    stored = json.dumps(server.session.messages)
    assert "alice@example.org" not in stored and "4111" not in stored
    assert "[EMAIL_1]" in stored and "[CARD_1]" in stored
    _chat(server, "and the next step")
    assert "alice@example.org" not in prompts[1] and "4111" not in prompts[1]
    assert "[EMAIL_1]" in prompts[1]


def test_private_chat_drops_tagged_answers_from_the_recap_and_history(server, monkeypatch):
    prompts: list[str] = []
    server.session.messages.extend([
        {"role": "user", "content": "check the mail"},
        {"role": "assistant", "content": "The merger closes Friday.", privacy.PRIV_TAG: ["mail"]},
    ])
    privacy.PrivacyStore().set_chat_private("default", True)

    def fake_stream_claude(task, **kwargs):
        prompts.append(task)
        return "ok"

    monkeypatch.setattr("dourmouse.code_backends.stream_claude", fake_stream_claude)
    _chat(server, "anything else")
    assert "merger" not in prompts[0] and "removed from this chat's context" in prompts[0]
    assert "merger" not in json.dumps(server.session.messages)
