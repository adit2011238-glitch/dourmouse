"""v5.5 Freebuff Desktop read-bridge + v5.11 dispatch tests
(freebuff_bridge.py + wiring).

Every test is hermetic (Rule 2.1): a tiny stdlib HTTP server under a free
port serves canned Freebuff-shaped JSON (GET + POST), and FREEBUFF_API_URL
points the bridge at it — no real Freebuff app, no network beyond loopback.
Verifies:

- freebuff_status / freebuff_account honest authed/unauthed/off states
- freebuff_projects / threads / thread messages / notes / skills / changes
  parse the REAL shapes the app serves (payloads captured live)
- path-like thread ids are refused (path-traversal guard)
- v5.11 dispatch: create thread + post message (real request bodies),
  honest NOT CONFIGURED when the app is unreachable, prompt/path guards,
  oversized-prompt cap
- honest NOT CONFIGURED when the app is unreachable (Rule 2.2)
- the freebuff subagent actually carries the new tools
- the panel payload (freebuff_panel_snapshot) and the SETUP row
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from dourmouse import connections as conn
from dourmouse import freebuff_bridge as fb
from dourmouse.general_roster import build_general_registry

AUTH_OK = {"authed": True, "user": {"id": "u1", "email": "adit@example.com", "name": "adit"}}
AUTH_NOT = {"authed": False}

PROJECTS = {
    "projects": [
        {
            "path": "/Users/adit/Documents/atlas",
            "threads": [
                {
                    "id": "8aec059f-207c-413c-b51f-0beec820685c",
                    "title": "ATLAS JARVIS — Unified Master Prompt\nv2.0",
                    "status": "open",
                    "turnState": "idle",
                    "lastTurnOutcome": "error",
                    "updatedAt": 1786032470716,
                },
                {
                    "id": "07e82ed6-c0cf-457e-ad7a-ffb36dcbb2a5",
                    "title": "daily ops routine",
                    "status": "open",
                    "turnState": "running",
                    "lastTurnOutcome": None,
                    "updatedAt": 1786032000000,
                },
            ],
        }
    ]
}

THREAD = {
    "thread": {"id": "8aec059f-207c-413c-b51f-0beec820685c"},
    "messages": [
        {
            "role": "user",
            "parts": [{"kind": "text", "text": "check my inbox"}],
        },
        {
            "role": "assistant",
            "parts": [
                {"kind": "text", "text": "You have 3 unread emails."},
                {"kind": "text", "text": " 2 are promotional."},
            ],
        },
    ],
}

NOTES = {"notes": [{"id": "n1", "title": "ideas", "body": "build the router"}, {"id": "n2", "title": "todos"}]}
SKILLS = {"skills": [{"name": "agent-browser", "prompt": "---\nname: agent-browser\ndescription: Browser automation CLI.\n"}]}
CHANGES = {
    "scope": "uncommitted",
    "branch": "main",
    "files": [
        {"path": "atlas/ops/cli.py", "status": "modified", "adds": 182, "dels": 5},
        {"path": ".mcp.json", "status": "added", "adds": 15, "dels": 0},
    ],
}
RECENTS = {"paths": ["/Users/adit/Documents/atlas"]}

# v5.11 dispatch responses (shapes captured live from the real app).
CREATE_THREAD = {
    "id": "9c1b2a3e-4d5f-6789-abcd-ef0123456789",
    "projectPath": "/Users/adit/Documents/atlas",
    "title": "test dispatch",
    "status": "open",
    "turnState": "idle",
}
POSTED = {"ok": True, "queued": False}


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        pass

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/auth/status":
            self._send(AUTH_OK)
        elif path == "/api/projects":
            self._send(PROJECTS)
        elif path == "/api/thread/8aec059f-207c-413c-b51f-0beec820685c":
            self._send(THREAD)
        elif path == "/api/notes":
            self._send(NOTES)
        elif path == "/api/skills":
            self._send(SKILLS)
        elif path == "/api/project/changes":
            self._send(CHANGES)
        elif path == "/api/project/recents":
            self._send(RECENTS)
        else:
            self._send({"error": "not found"})

    def do_POST(self):
        # Record every request so tests can assert the exact bodies the
        # bridge sent and their order (deterministic, Rule 2.8).
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        body = json.loads(raw) if raw else {}
        entry = {"path": self.path, "body": body}
        _FakeHandler.last_post = entry
        _FakeHandler.last_posts.append(entry)
        if self.path == "/api/threads":
            self._send(CREATE_THREAD)
        elif self.path.startswith("/api/thread/") and self.path.endswith("/message"):
            self._send(POSTED)
        else:
            self._send({"error": "not found"}, status=404)


# Per-test recording slots (the handler is shared across tests via the
# module-scoped fixture, so tests read the LAST recorded request).
_FakeHandler.last_post = None  # type: ignore[attr-defined]
_FakeHandler.last_posts = []  # type: ignore[attr-defined]


class _FailHandler(BaseHTTPRequestHandler):
    """Answers every POST with HTTP 500 so the honest-failure path is tested
    against a LIVE server, not just a dead port."""

    def log_message(self, fmt, *args):  # quieter
        pass

    def do_POST(self):
        body = b'{"error": "boom"}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PostFailHandler(BaseHTTPRequestHandler):
    """Create succeeds, but every /message post fails — the orphan-thread
    path (thread created, prompt rejected)."""

    def log_message(self, fmt, *args):  # quieter
        pass

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/threads":
            self._send(CREATE_THREAD)
        else:
            self._send({"error": "boom"}, status=500)


@pytest.fixture(scope="module")
def fake_freebuff():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def fb_url(fake_freebuff, monkeypatch):
    monkeypatch.setattr(fb, "_FREEBUFF_BASE", fake_freebuff)
    return fake_freebuff


@pytest.fixture(scope="module")
def fail_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def fail_url(fail_server, monkeypatch):
    monkeypatch.setattr(fb, "_FREEBUFF_BASE", fail_server)
    return fail_server


@pytest.fixture(scope="module")
def post_fail_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PostFailHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def post_fail_url(post_fail_server, monkeypatch):
    monkeypatch.setattr(fb, "_FREEBUFF_BASE", post_fail_server)
    return post_fail_server


class TestAccount:
    def test_authed_account(self, fb_url):
        assert fb.freebuff_account() == {"email": "adit@example.com", "name": "adit"}

    def test_status_ok(self, fb_url):
        st = fb.freebuff_status()
        assert st["ok"] is True
        assert st["account"]["email"] == "adit@example.com"

    def test_app_not_running_honest(self, monkeypatch):
        # Point the bridge at a dead port -> honest NOT CONFIGURED, no crash.
        monkeypatch.setattr(fb, "_FREEBUFF_BASE", "http://127.0.0.1:1")
        assert fb.freebuff_account() is None
        st = fb.freebuff_status()
        assert st["ok"] is False
        assert "not" in st["detail"].lower()


class TestProjects:
    def test_projects_shape(self, fb_url):
        projects = fb.freebuff_projects()
        assert len(projects) == 1
        p = projects[0]
        assert p["path"].endswith("atlas")
        assert p["thread_count"] == 2
        assert p["threads"][0]["id"] == "8aec059f-207c-413c-b51f-0beec820685c"
        assert p["threads"][0]["status"] == "open"

    def test_thread_messages(self, fb_url):
        msgs = fb.freebuff_thread_messages("8aec059f-207c-413c-b51f-0beec820685c")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert "3 unread" in msgs[1]["text"]

    def test_path_like_thread_id_refused(self, fb_url):
        with pytest.raises(ValueError):
            fb.freebuff_thread_messages("../../etc/passwd")
        with pytest.raises(ValueError):
            fb.freebuff_thread_messages("")

    def test_notes_skills_changes(self, fb_url):
        assert [n["title"] for n in fb.freebuff_notes()] == ["ideas", "todos"]
        assert fb.freebuff_skills()[0]["name"] == "agent-browser"
        changes = fb.freebuff_project_changes("/Users/adit/Documents/atlas")
        assert changes[0]["status"] == "modified"

    def test_relative_change_path_refused(self, fb_url):
        with pytest.raises(ValueError):
            fb.freebuff_project_changes("relative/path")


class TestTools:
    def test_status_tool_not_configured(self, monkeypatch):
        monkeypatch.setattr(fb, "_FREEBUFF_BASE", "http://127.0.0.1:1")
        text = fb._freebuff_status_tool({})
        assert "NOT CONFIGURED" in text

    def test_threads_tool(self, fb_url):
        text = fb._freebuff_threads_tool({})
        assert "FREEBUFF THREADS" in text
        assert "daily ops routine" in text

    def test_read_thread_tool_requires_id(self, fb_url):
        assert "requires 'thread_id'" in fb._freebuff_read_thread_tool({})

    def test_read_thread_tool(self, fb_url):
        text = fb._freebuff_read_thread_tool(
            {"thread_id": "8aec059f-207c-413c-b51f-0beec820685c"}
        )
        assert "[USER]" in text and "[AI  ]" in text

    def test_changes_tool(self, fb_url):
        text = fb._freebuff_changes_tool({"path": "/Users/adit/Documents/atlas"})
        assert "modified" in text


class TestDispatch:
    """v5.11 write bridge — create thread + post message against the fake."""

    def test_create_thread_builds_correct_request(self, fb_url):
        thread = fb.freebuff_create_thread(
            "/Users/adit/Documents/atlas", title="test dispatch"
        )
        assert thread["id"] == CREATE_THREAD["id"]
        req = _FakeHandler.last_post
        assert req["path"] == "/api/threads"
        assert req["body"] == {
            "projectPath": "/Users/adit/Documents/atlas",
            "title": "test dispatch",
        }

    def test_create_thread_refuses_relative_path(self, fb_url):
        with pytest.raises(fb.FreebuffDispatchError):
            fb.freebuff_create_thread("relative/path")

    def test_post_message_builds_correct_request(self, fb_url):
        posted = fb.freebuff_post_message(
            CREATE_THREAD["id"], "do the thing", "/Users/adit/Documents/atlas"
        )
        assert posted == POSTED
        req = _FakeHandler.last_post
        assert req["path"] == f"/api/thread/{CREATE_THREAD['id']}/message"
        assert req["body"]["text"] == "do the thing"
        assert req["body"]["projectPath"] == "/Users/adit/Documents/atlas"

    def test_post_message_requires_prompt(self, fb_url):
        with pytest.raises(fb.FreebuffDispatchError):
            fb.freebuff_post_message(CREATE_THREAD["id"], "  ", "/Users/adit/Documents/atlas")

    def test_post_message_caps_oversized_prompt(self, fb_url):
        with pytest.raises(fb.FreebuffDispatchError, match="too long"):
            fb.freebuff_post_message(
                CREATE_THREAD["id"], "x" * (fb._MAX_DISPATCH_CHARS + 1),
                "/Users/adit/Documents/atlas",
            )

    def test_post_message_refuses_path_like_thread_id(self, fb_url):
        with pytest.raises(fb.FreebuffDispatchError):
            fb.freebuff_post_message("../../etc/passwd", "hi", "/Users/adit/Documents/atlas")

    def test_dispatch_creates_then_posts(self, fb_url):
        _FakeHandler.last_posts.clear()  # shared module-scoped server
        out = fb.freebuff_dispatch(
            "do the thing", "/Users/adit/Documents/atlas", title="test dispatch"
        )
        assert out["thread"]["id"] == CREATE_THREAD["id"]
        assert out["posted"]["ok"] is True
        posts = [r for r in getattr(_FakeHandler, "last_posts", [])]
        # create + message each happened exactly once, in order
        assert len(posts) == 2
        assert posts[0]["path"] == "/api/threads"
        assert posts[1]["path"].endswith("/message")

    def test_dispatch_tool_ok(self, fb_url):
        text = fb._freebuff_dispatch_tool(
            {
                "prompt": "do the thing",
                "project_path": "/Users/adit/Documents/atlas",
                "title": "test dispatch",
            }
        )
        assert CREATE_THREAD["id"] in text
        assert "accepted (running)" in text
        assert "freebuff_read_thread" in text

    def test_dispatch_tool_requires_prompt_and_path(self, fb_url):
        assert "non-empty 'prompt'" in fb._freebuff_dispatch_tool({})
        assert "project_path" in fb._freebuff_dispatch_tool({"prompt": "hi"})

    def test_dispatch_tool_honest_app_error(self, fail_url):
        text = fb._freebuff_dispatch_tool(
            {"prompt": "hi", "project_path": "/Users/adit/Documents/atlas"}
        )
        assert "FAILED" in text and "HTTP 500" in text

    def test_dispatch_tool_honest_app_down(self, monkeypatch):
        monkeypatch.setattr(fb, "_FREEBUFF_BASE", "http://127.0.0.1:1")
        text = fb._freebuff_dispatch_tool(
            {"prompt": "hi", "project_path": "/Users/adit/Documents/atlas"}
        )
        assert "FAILED" in text and "not reachable" in text

    def test_dispatch_validates_prompt_before_creating(self, fb_url):
        """An oversized/empty prompt must fail WITHOUT ever hitting
        /api/threads — no orphan thread for a predictably-bad prompt."""
        _FakeHandler.last_posts.clear()
        with pytest.raises(fb.FreebuffDispatchError, match="too long"):
            fb.freebuff_dispatch(
                "x" * (fb._MAX_DISPATCH_CHARS + 1),
                "/Users/adit/Documents/atlas",
            )
        assert _FakeHandler.last_posts == []

    def test_dispatch_post_failure_names_created_thread(self, post_fail_url):
        """Create succeeds, post 500s -> the error must carry the created
        thread id so the caller can honestly report the orphan."""
        with pytest.raises(fb.FreebuffDispatchError) as exc_info:
            fb.freebuff_dispatch("do the thing", "/Users/adit/Documents/atlas")
        msg = str(exc_info.value)
        assert CREATE_THREAD["id"] in msg
        assert "was created but the prompt failed to post" in msg

    def test_dispatch_tool_post_failure_names_created_thread(self, post_fail_url):
        text = fb._freebuff_dispatch_tool(
            {"prompt": "do the thing", "project_path": "/Users/adit/Documents/atlas"}
        )
        assert "FAILED" in text
        assert CREATE_THREAD["id"] in text
        assert "was created but the prompt failed to post" in text


class TestRosterAndPanel:
    def test_freebuff_subagent_registered(self):
        registry = build_general_registry()
        sub = registry.get_subagent("freebuff")
        assert sub is not None
        names = {t.name for t in sub.tools}
        assert {
            "freebuff_status",
            "freebuff_projects",
            "freebuff_threads",
            "freebuff_read_thread",
            "freebuff_notes",
            "freebuff_skills",
            "freebuff_changes",
            "freebuff_dispatch",
        } <= names

    def test_panel_snapshot_not_configured(self, monkeypatch):
        monkeypatch.setattr(fb, "_FREEBUFF_BASE", "http://127.0.0.1:1")
        snap = fb.freebuff_panel_snapshot()
        assert snap["configured"] is False
        assert snap["detail"]

    def test_panel_snapshot_live(self, fb_url):
        snap = fb.freebuff_panel_snapshot()
        assert snap["configured"] is True
        assert snap["account"]["email"] == "adit@example.com"
        assert snap["project_count"] == 1
        assert snap["thread_count"] == 2
        assert snap["notes_count"] == 2
        assert snap["skills_count"] == 1


class TestConnectionsWiring:
    def test_connections_uses_real_probe(self, fake_freebuff, monkeypatch):
        # autouse fixture forces _tcp_reachable False; override for the UI port.
        monkeypatch.setattr(
            conn,
            "_tcp_reachable",
            lambda host, port, timeout=0.6: port == conn._FREEBUFF_UI_PORT,
        )
        # Point the bridge (which connections calls) at the fake server.
        monkeypatch.setattr(fb, "_FREEBUFF_BASE", fake_freebuff)
        report = conn.check_connections()
        assert report["freebuff"]["ok"] is True
        assert "app running" in report["freebuff"]["detail"]
        # account email never leaks into the report
        assert "adit@example.com" not in json.dumps(report)
