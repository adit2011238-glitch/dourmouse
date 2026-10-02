"""Phase J: the shared desk. Every chat screen can reach the browser, the file
preview and the player tools; the preview opens text, markdown, csv, json and
code; secret files are refused; the CODE bridge exposes the whole desk.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

import pytest

from dourmouse.dispatch import (
    SHARED_DESK_TOOLS,
    DispatchRegistry,
    Subagent,
    run_dispatch,
    run_dispatch_messages,
    system_message,
)
from dourmouse.general_roster import build_general_registry
from dourmouse.tests.test_dispatch import FakeClient, _FakeMessage, _FakeResponse
from dourmouse.webui import _record_player_open, _render_text_view, _text_view_path, run_server

# One entry per chat screen: (screen, pinned agent or None). The pins are the
# agents each screen routes to in ui/console.html; HOME is unpinned.
_SCREENS = [
    ("HOME", None),
    ("COMMS", "mail"),
    ("RESEARCH", "research_info"),
    ("MEDIA", "media"),
    ("CODE", "code_ollama"),
    ("NEWS", "news"),
    ("AGENTSMITH", "agent_smith"),
]

_EMPTY_STATE = {"path": "", "kind": "", "opened_at": 0.0, "last_command": None}


def _sent_tool_names(client: FakeClient) -> set[str]:
    return {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}


class TestEveryChatScreenReachesTheDesk:
    @pytest.mark.parametrize("screen,agent", _SCREENS, ids=[s[0] for s in _SCREENS])
    def test_screen_gets_the_desk_and_keeps_its_pin(self, monkeypatch, screen, agent):
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        registry = build_general_registry()
        client = FakeClient([_FakeResponse(_FakeMessage(content="ok"))])
        if agent is None:
            run_dispatch("check my inbox", registry, client=client)
        else:
            messages = [
                {"role": "system", "content": system_message(registry)},
                {"role": "user", "content": "open the thing"},
            ]
            run_dispatch_messages(messages, registry, client=client, forced_agent=agent)
        names = _sent_tool_names(client)
        missing = [t for t in SHARED_DESK_TOOLS if t not in names]
        assert not missing, f"{screen} is missing desk tools {missing}"
        if agent is not None:
            # Pinning is intact: no delegation to another agent, and the
            # pinned agent's own tools are still offered.
            assert "delegate_task" not in names
            own = {t.name for sub in registry.all_subagents() if sub.name == agent for t in sub.tools}
            assert own <= names

    def test_a_pinned_run_can_actually_call_a_desk_tool(self, monkeypatch):
        # Offering the schema is not enough: the call must execute. A relative
        # path makes the real handler answer without touching any server.
        monkeypatch.setenv("DOURMOUSE_FAST_LANE", "0")
        from dourmouse.tests.test_dispatch import _FakeToolCall

        registry = build_general_registry()
        call = _FakeToolCall("c1", "open_file_preview", json.dumps({"path": "relative.txt"}))
        client = FakeClient([
            _FakeResponse(_FakeMessage(tool_calls=[call])),
            _FakeResponse(_FakeMessage(content="done")),
        ])
        messages = [
            {"role": "system", "content": system_message(registry)},
            {"role": "user", "content": "show relative.txt"},
        ]
        run_dispatch_messages(messages, registry, client=client, forced_agent="research_info")
        second = client.chat.completions.calls[1]["messages"]
        assert any("requires an ABSOLUTE path" in str(m.get("content")) for m in second if m.get("role") == "tool")

    def test_a_registry_without_the_tools_does_not_invent_them(self):
        reg = DispatchRegistry()
        reg.register_subagent(Subagent(name="solo", domain="t", description="d", tools=()))
        from dourmouse.dispatch import shared_desk_specs

        assert shared_desk_specs(reg) == []


class TestCodeBridge:
    def test_the_bridge_exposes_the_whole_desk(self):
        from dourmouse.mcp_bridge import exposed_tools, missing_shared_desk_tools

        tools = exposed_tools(build_general_registry())
        assert missing_shared_desk_tools(tools) == []

    def test_missing_tools_are_reported_by_name(self):
        from dourmouse.mcp_bridge import missing_shared_desk_tools

        assert missing_shared_desk_tools([]) == list(SHARED_DESK_TOOLS)

    def test_a_code_chat_is_told_the_desk_exists(self):
        from dourmouse.code_backends import _SHARED_DESK_HINT

        for name in SHARED_DESK_TOOLS:
            assert name in _SHARED_DESK_HINT


# ---------------------------------------------------------------- text preview


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="echo_agent", domain="test", description="d", tools=()))
    srv = run_server(reg, port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _view(port, path):
    return _request(port, "GET", "/api/files/text-view?path=" + urllib.parse.quote(str(path)))


class TestTextPreviewRoute:
    def test_txt(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "notes.txt"
        f.write_text("hello <b>world</b>")
        status, body = _view(port, f)
        assert status == 200
        assert b"hello &lt;b&gt;world&lt;/b&gt;" in body  # escaped, never live markup

    def test_markdown_is_shown_as_source(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "readme.md"
        f.write_text("# Title\n\n- item\n")
        status, body = _view(port, f)
        assert status == 200 and b"# Title" in body

    def test_csv_becomes_a_table(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "data.csv"
        f.write_text("a,b\n1,2\n3,<x>\n")
        status, body = _view(port, f)
        assert status == 200
        assert b"<th>a</th>" in body and b"<td>1</td>" in body and b"<td>&lt;x&gt;</td>" in body

    def test_json_is_pretty_printed(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "d.json"
        f.write_text('{"k":[1,2]}')
        status, body = _view(port, f)
        assert status == 200 and b"&quot;k&quot;: [" in body

    def test_code(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "x.py"
        f.write_text("print('hi')\n")
        status, body = _view(port, f)
        assert status == 200 and b"print(" in body

    def test_large_file_is_cut_and_says_so(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "big.log"
        f.write_text("x" * (600 * 1024))
        status, body = _view(port, f)
        assert status == 200
        assert b"Showing only the first part" in body
        assert len(body) < 600 * 1024

    def test_binary_content_is_not_shown(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "blob.txt"
        f.write_bytes(b"ab\x00cd")
        status, body = _view(port, f)
        assert status == 200 and b"looks binary" in body

    def test_response_is_sandboxed(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "a.txt"
        f.write_text("x")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/files/text-view?path=" + urllib.parse.quote(str(f)))
        resp = conn.getresponse()
        resp.read()
        assert resp.getheader("Content-Security-Policy") == "sandbox"
        conn.close()

    def test_secret_files_are_refused_by_the_route(self, server, tmp_path):
        _srv, port = server
        for name in (".env", "server.pem", "id_rsa", ".env.local"):
            f = tmp_path / name
            f.write_text("SECRET=1")
            status, body = _view(port, f)
            assert status == 400 and b"SECRET" not in body
        # A text-looking file inside a credentials directory is refused by the
        # deny-list itself, not by the extension filter.
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        k = ssh / "config.txt"
        k.write_text("SECRET=1")
        status, body = _view(port, k)
        assert status == 400 and b"SECRET" not in body

    def test_unsupported_or_relative_paths_are_refused(self, server, tmp_path):
        _srv, port = server
        f = tmp_path / "a.bin"
        f.write_text("x")
        assert _view(port, f)[0] == 400
        assert _view(port, "relative.txt")[0] == 400
        assert _view(port, tmp_path / "missing.txt")[0] == 400

    def test_helpers_directly(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert _text_view_path(str(f)) == f.resolve()
        assert _text_view_path("") is None
        assert b"<pre>x</pre>" in _render_text_view(f)


class TestOpenFilePreviewTool:
    @pytest.fixture
    def pane_calls(self, monkeypatch):
        calls: list[dict] = []

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake(req, timeout=None):
            calls.append(json.loads(req.data.decode()))
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", _fake)
        return calls

    @pytest.mark.parametrize("name", ["a.txt", "a.md", "a.csv", "a.json", "a.py"])
    def test_text_types_open_in_the_text_viewer(self, tmp_path, pane_calls, name):
        from dourmouse.system_access import _open_file_preview_tool

        f = tmp_path / name
        f.write_text("data")
        out = _open_file_preview_tool({"path": str(f)})
        assert out.startswith("OPENED TEXT PREVIEW:")
        assert pane_calls[0]["url"].startswith("/api/files/text-view?path=")

    @pytest.mark.parametrize("name", [".env", "key.pem", "id_rsa", ".env.production"])
    def test_secret_files_are_refused_and_nothing_is_opened(self, tmp_path, pane_calls, name):
        from dourmouse.system_access import _open_file_preview_tool

        f = tmp_path / name
        f.write_text("TOKEN=abc")
        out = _open_file_preview_tool({"path": str(f)})
        assert out.startswith("REFUSED")
        assert pane_calls == []

    def test_a_text_file_in_a_credentials_dir_is_refused(self, tmp_path, pane_calls):
        from dourmouse.system_access import _open_file_preview_tool

        d = tmp_path / ".aws"
        d.mkdir()
        f = d / "notes.txt"
        f.write_text("x")
        out = _open_file_preview_tool({"path": str(f)})
        assert out.startswith("REFUSED") and "deny-list" in out
        assert pane_calls == []

    def test_pdf_and_mp3_still_use_the_existing_preview_page(self, tmp_path, pane_calls):
        from dourmouse.system_access import _open_file_preview_tool

        for name in ("a.pdf", "a.mp3"):
            f = tmp_path / name
            f.write_bytes(b"x")
            _open_file_preview_tool({"path": str(f)})
        assert all(c["url"].startswith("/file_preview.html?") for c in pane_calls)


# ---------------------------------------------------------------- player tools


class TestPlayerTools:
    def test_nothing_opened_is_reported_honestly(self, server, monkeypatch):
        import dourmouse.webui as webui
        from dourmouse.system_access import _player_now_playing_tool, _player_pause_tool

        _srv, port = server
        monkeypatch.setenv("DOURMOUSE_UI_PORT", str(port))
        monkeypatch.setattr(webui, "_PLAYER_STATE", dict(_EMPTY_STATE))
        assert _player_now_playing_tool({}).startswith("NOTHING OPENED")
        out = _player_pause_tool({})
        assert out.startswith("ERROR") and "nothing is open" in out

    def test_open_then_control_is_sent_but_never_claimed_as_done(self, server, monkeypatch, tmp_path):
        import dourmouse.webui as webui
        from dourmouse.system_access import (
            _player_now_playing_tool,
            _player_pause_tool,
            _player_play_tool,
            _player_seek_tool,
        )

        srv, port = server
        monkeypatch.setenv("DOURMOUSE_UI_PORT", str(port))
        monkeypatch.setattr(webui, "_PLAYER_STATE", dict(_EMPTY_STATE))
        clip = tmp_path / "song.mp3"
        clip.write_bytes(b"x")
        _record_player_open("/file_preview.html?src=files&path=" + urllib.parse.quote(str(clip)))

        events: list[dict] = []
        monkeypatch.setattr(srv.events_broadcast, "broadcast", events.append)
        now = _player_now_playing_tool({})
        assert "song.mp3" in now and "NOT known" in now
        for fn in (_player_play_tool, _player_pause_tool):
            assert fn({}).startswith("NOT CONFIRMED")
        assert _player_seek_tool({"seconds": 42}).startswith("NOT CONFIRMED")
        assert [e["action"] for e in events] == ["play", "pause", "seek"]
        assert events[-1]["seconds"] == 42 and events[-1]["path"] == str(clip)

    def test_bad_seek_arguments_are_rejected_before_any_call(self):
        from dourmouse.system_access import _player_seek_tool

        assert _player_seek_tool({}).startswith("ERROR")
        assert _player_seek_tool({"seconds": -3}).startswith("ERROR")
        assert _player_seek_tool({"seconds": "abc"}).startswith("ERROR")

    def test_non_media_open_does_not_become_now_playing(self, monkeypatch):
        import dourmouse.webui as webui

        monkeypatch.setattr(webui, "_PLAYER_STATE", dict(_EMPTY_STATE))
        _record_player_open("/file_preview.html?src=files&path=" + urllib.parse.quote("/tmp/a.pdf"))
        _record_player_open("https://example.com")
        assert webui._PLAYER_STATE["path"] == ""

    def test_server_rejects_an_unknown_action(self, server):
        _srv, port = server
        status, _ = _request(port, "POST", "/api/player/control", {"action": "explode"})
        assert status == 400
