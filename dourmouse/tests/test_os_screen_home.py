"""Finding #144: the HOME screen's pure helpers (node) and its backend (os_api/home.py).

HOME's DOM code is verified live in a real browser (EVIDENCE/144_*); what can be
checked without a browser is checked here: the turn header maths, what the chip
colours mean, which ledger records belong on a thread, and the three routes.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse.general_roster import build_general_registry
from dourmouse.os_api import home as home_api

_ROOT = Path(__file__).resolve().parents[2]
_HOME = _ROOT / "ui" / "assets" / "os" / "screens" / "home"
_KIT = _ROOT / "ui" / "assets" / "os" / "kit"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_KIT / 'thread-helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHelpers:
    def test_header_counts_only_real_tool_steps_and_real_elapsed_time(self, tmp_path):
        out = node(tmp_path, """
const base = { status: 'done', steps: [{ kind: 'plan' }, { kind: 'tool', name: 'a' }, { kind: 'tool', name: 'b' }, { kind: 'error' }], elapsedMs: 2412, model: '', startedAt: 0 };
R.done = h.headerText(base);
R.one = h.headerText({ ...base, steps: [{ kind: 'tool', name: 'a' }], elapsedMs: 900 });
R.none = h.headerText({ ...base, steps: [], elapsedMs: null, status: 'done' });
R.live = h.headerText({ ...base, status: 'working', elapsedMs: null, startedAt: 1000 }, 4500);
R.stopped = h.headerText({ ...base, status: 'stopped' });
R.failed = h.headerText({ ...base, status: 'error', model: 'glm-5' });
""")
        assert out["done"] == "Dourmouse · 2 steps · 2.4s"
        assert out["one"] == "Dourmouse · 1 step · 0.9s"
        assert out["none"] == "Dourmouse", "no time is shown when none was measured"
        assert out["live"] == "Dourmouse · 2 steps · 3.5s · working"
        assert out["stopped"].endswith("stopped") and out["failed"].endswith("failed · glm-5")

    def test_chip_tone_reflects_running_and_failed_calls(self, tmp_path):
        out = node(tmp_path, """
R.tones = [{ running: true }, { running: false, ok: false }, { running: false, result: 'ERROR: x' }, { running: false, result: 'fine' }].map(h.chipTone);
R.live = ['thinking', 'generating', 'working', 'waiting', 'done', 'stopped', 'error'].map((s) => h.isLive({ status: s }));
R.copy = [h.copyText({ reply: 'raw **text**' }), h.copyText({})];
""")
        assert out["tones"] == ["warn", "bad", "bad", ""]
        assert out["live"] == [True, True, True, True, False, False, False]
        assert out["copy"] == ["raw **text**", ""]

    def test_follow_only_when_the_reader_is_near_the_bottom(self, tmp_path):
        out = node(tmp_path, "R.a = h.shouldFollow({ scrollHeight: 1000, scrollTop: 900, clientHeight: 100 }); R.b = h.shouldFollow({ scrollHeight: 1000, scrollTop: 300, clientHeight: 100 });")
        assert out == {"a": True, "b": False}

    def test_only_home_records_go_on_home_and_old_records_count_as_home(self, tmp_path):
        out = node(tmp_path, "R.n = h.homeRecords([{ screen: 'HOME', n: 1 }, { n: 2 }, { screen: 'CODE', n: 3 }, { screen: 'NEWS', n: 4 }]).map((t) => t.n); R.empty = h.homeRecords(null);")
        assert out == {"n": [1, 2], "empty": []}

    def test_every_thread_screen_reads_back_only_its_own_records(self, tmp_path):
        out = node(tmp_path, "const recs = [{ screen: 'HOME', n: 1 }, { n: 2 }, { screen: 'CODE', n: 3 }, { screen: 'NEWS', n: 4 }, { screen: 'CODE', n: 5 }]; R.code = h.recordsFor(recs, 'CODE').map((t) => t.n); R.news = h.recordsFor(recs, 'NEWS').map((t) => t.n); R.none = h.recordsFor(recs, 'MEDIA');")
        assert out == {"code": [3, 5], "news": [4], "none": []}


def _home_source() -> str:
    """HOME's own file plus the shared thread view it draws the conversation with."""
    return (_HOME / "index.js").read_text(encoding="utf-8") + (_KIT / "thread-view.js").read_text(encoding="utf-8")


class TestHomeSourceRules:
    def test_home_has_no_sample_turns_and_no_invented_numbers(self):
        src = _home_source()
        for sample in ("play the verification clip", "Risk score", "which of those four", "2.4s", "0.9s"):
            assert sample not in src, f"a mockup sample string leaked into HOME: {sample}"

    def test_prompt_text_reaches_the_dom_only_as_text(self):
        src = _home_source()
        # the model's reply goes through the escaping markdown renderer; user text and tool output through textContent
        assert "setHtml(reply, md(turn.reply))" in src
        assert "el('div', 'you', turn.text)" in src

    def test_shelve_is_not_offered_because_it_has_no_backend(self):
        src = _home_source()
        assert "SHELVE" not in src


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestScreensManifest:
    def test_lists_the_folders_that_have_an_index(self, server):
        status, data = call(server, "GET", "/api/os/screens")
        assert status == 200 and data["ok"] is True
        assert {"home", "security"} <= set(data["built"]) and data["built"] == sorted(data["built"])

    def test_a_folder_without_an_index_or_with_a_bad_name_is_not_built(self, tmp_path):
        (tmp_path / "news").mkdir()
        (tmp_path / "news" / "index.js").write_text("export default {}", encoding="utf-8")
        (tmp_path / "empty").mkdir()
        (tmp_path / "Bad Name").mkdir()
        (tmp_path / "Bad Name" / "index.js").write_text("", encoding="utf-8")
        (tmp_path / "file.js").write_text("", encoding="utf-8")
        assert home_api.built_screens(tmp_path) == ["news"]
        assert home_api.built_screens(tmp_path / "missing") == []


class TestSessionRoutes:
    def _tab(self, srv, tab, ledger=None, locked=False):
        ws = Path(__import__("os").environ["DOURMOUSE_WORKSPACE"])
        sessions = ws / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        session_file = sessions / f"session_{tab}.jsonl"
        if ledger is not None:
            session_file.write_text("\n".join(json.dumps(r) for r in ledger) + "\n", encoding="utf-8")
        lock = threading.Lock()
        if locked:
            lock.acquire()
        srv.sessions_by_tab[tab] = SimpleNamespace(session_file=session_file)
        srv.gates_by_tab[tab] = object()
        srv.locks_by_tab[tab] = lock
        return lock

    def test_a_tab_with_no_conversation_answers_200_with_no_turns(self, server):
        status, data = call(server, "GET", "/api/os/session?tab_id=fresh-tab")
        assert status == 200 and data["ok"] is True and data["turns"] == []

    def test_the_tabs_own_ledger_is_read_back_with_its_screen_field(self, server):
        rec = {"turn": 1, "timestamp": "2026-09-26T10:00:00", "elapsed_ms": 900, "user": "wrapped", "display_text": "hello", "screen": "CODE", "final_text": "hi", "transcript": []}
        self._tab(server, "tab-a", [rec])
        status, data = call(server, "GET", "/api/os/session?tab_id=tab-a")
        assert status == 200 and data["ok"]
        assert data["turns"][0]["display_text"] == "hello" and data["turns"][0]["screen"] == "CODE"

    def test_a_session_that_has_not_written_a_turn_yet_is_empty_not_an_error(self, server):
        self._tab(server, "tab-b", None)
        status, data = call(server, "GET", "/api/os/session?tab_id=tab-b")
        assert status == 200 and data["turns"] == []

    def test_a_missing_or_malformed_tab_id_is_refused_with_a_reason(self, server):
        assert call(server, "GET", "/api/os/session")[0] == 400
        status, data = call(server, "GET", "/api/os/session?tab_id=../../etc")
        assert status == 400 and "not allowed" in data["error"]
        assert call(server, "POST", "/api/os/session/new", {"tab_id": "a b"})[0] == 400

    def test_new_thread_forgets_the_tabs_session_and_leaves_the_file(self, server):
        self._tab(server, "tab-c", [{"turn": 1, "user": "x", "final_text": "y"}])
        ledger = Path(server.sessions_by_tab["tab-c"].session_file)
        status, data = call(server, "POST", "/api/os/session/new", {"tab_id": "tab-c"})
        assert status == 200 and data["ok"] and data["previous"] == "session_tab-c"
        assert "tab-c" not in server.sessions_by_tab and "tab-c" not in server.gates_by_tab and "tab-c" not in server.locks_by_tab
        assert ledger.is_file(), "the old session file stays on disk"
        again = call(server, "POST", "/api/os/session/new", {"tab_id": "tab-c"})
        assert again[0] == 200 and again[1]["previous"] is None

    def test_new_thread_is_refused_while_a_turn_is_running(self, server):
        lock = self._tab(server, "tab-d", [], locked=True)
        try:
            status, data = call(server, "POST", "/api/os/session/new", {"tab_id": "tab-d"})
            assert status == 409 and "still running" in data["error"]
            assert "tab-d" in server.sessions_by_tab, "a refused rotation must not drop the session"
        finally:
            lock.release()
