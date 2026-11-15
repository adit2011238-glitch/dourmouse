"""Projects screens (plan 1.6, finding 202): the pure helpers under node, the rules the screen files must keep, and the
route sequence the screen performs against the real store (create, thread, save a turn, read it back, delete)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "projects"
_KIT = _ROOT / "ui" / "assets" / "os" / "kit"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestAddress:
    def test_the_hash_names_the_project_and_the_thread(self, tmp_path):
        out = node(tmp_path, """
R.list = h.parseHash('#/projects'); R.one = h.parseHash('#/projects/alpha-1a2b3c');
R.thread = h.parseHash('#/projects/alpha-1a2b3c/9f8e7d'); R.other = h.parseHash('#/home/x/y');
R.bad = h.parseHash('#/projects/..%2F/x'); R.orphan = h.parseHash('#/projects//t1');
R.back = [h.hashFor(), h.hashFor('p1'), h.hashFor('p1', 't1'), h.hashFor('', 't1')];
R.tab = h.projectTab('p1', 't1'); R.isTab = [h.isProjectTab('pj:p1:t1'), h.isProjectTab('quick:abc'), h.isProjectTab('pj:p1'), h.isProjectTab('')];""")
        assert out["list"] == {"pid": "", "tid": ""} and out["one"] == {"pid": "alpha-1a2b3c", "tid": ""}
        assert out["thread"] == {"pid": "alpha-1a2b3c", "tid": "9f8e7d"} and out["other"] == {"pid": "", "tid": ""}
        assert out["bad"] == {"pid": "", "tid": ""} and out["orphan"]["tid"] == ""
        assert out["back"] == ["#/projects", "#/projects/p1", "#/projects/p1/t1", "#/projects"]
        assert out["tab"] == "pj:p1:t1" and out["isTab"] == [True, False, False, False]


class TestLines:
    def test_cover_is_a_token_and_never_a_gradient_or_a_free_colour(self, tmp_path):
        out = node(tmp_path, """
R.set = h.coverVar({ cover: '4' }); R.spread = h.coverVar({ id: 'zzz-1' }); R.again = h.coverVar({ id: 'zzz-1' });
R.junk = h.coverVar({ cover: 'red', id: 'a' }); R.none = h.coverVar(null);""")
        assert out["set"] == "var(--tk-cover-4)" and out["spread"] == out["again"]
        for key in ("spread", "junk", "none"):
            assert re.fullmatch(r"var\(--tk-cover-[1-6]\)", out[key])

    def test_meta_line_leaves_out_zero_parts_and_never_invents_a_number(self, tmp_path):
        out = node(tmp_path, """
R.both = h.metaLine({ open_work: 2, threads: 3 }); R.one = h.metaLine({ open_work: 0, threads: 1 });
R.none = h.metaLine({ open_work: 0, threads: 0 }); R.junk = h.metaLine(null); R.nan = h.metaLine({ open_work: 'x' });""")
        assert out == {"both": "2 open, 3 threads", "one": "1 thread", "none": "", "junk": "", "nan": ""}

    def test_filter_matches_name_and_goal_only_and_skips_broken_rows(self, tmp_path):
        out = node(tmp_path, """
const list = [{ id: 'a', name: 'Thesis', goal: 'Chapter drafts' }, { id: 'b', name: 'Tax', goal: '' }, null, { name: 'noid' }];
R.all = h.filterProjects(list, '').map((p) => p.id); R.name = h.filterProjects(list, 'the').map((p) => p.id);
R.goal = h.filterProjects(list, 'CHAPTER').map((p) => p.id); R.none = h.filterProjects(list, 'zzz').length; R.junk = h.filterProjects('x', 'a').length;""")
        assert out == {"all": ["a", "b"], "name": ["a"], "goal": ["a"], "none": 0, "junk": 0}

    def test_small_formatters(self, tmp_path):
        out = node(tmp_path, """
R.agent = [h.agentLabel('mail_calendar'), h.agentLabel('coder'), h.agentLabel('')];
R.size = [h.sizeLabel(12), h.sizeLabel(1500), h.sizeLabel(48000), h.sizeLabel(2500000), h.sizeLabel(-1), h.sizeLabel('x')];
R.title = [h.threadTitle('  hello   world '), h.threadTitle(''), h.threadTitle('x'.repeat(100)).length];
R.line = [h.threadLine({ summary: ' a  b ' }, 'x'), h.threadLine({}, '3m ago'), h.threadLine({ summary: 'y'.repeat(300) }, '').length];""")
        assert out["agent"] == ["Mail calendar", "Coder", ""]
        assert out["size"] == ["12 B", "1.5 KB", "48 KB", "2.5 MB", "", ""]
        assert out["title"] == ["hello world", "New thread", 60] and out["line"] == ["a b", "3m ago", 140]


class TestChecks:
    def test_name_and_text_are_checked_before_anything_is_sent(self, tmp_path):
        out = node(tmp_path, """
R.name = [h.validateName('  Thesis ').name, h.validateName('').ok, h.validateName('x'.repeat(121)).ok, h.validateName('a\\u0007b').ok];
R.text = [h.validateText('  hi ', 'note', 10).text, h.validateText('', 'note', 10).ok, h.validateText('x'.repeat(11), 'note', 10).message];""")
        assert out["name"] == ["Thesis", False, False, False]
        assert out["text"][0] == "hi" and out["text"][1] is False and "limit is 10" in out["text"][2]

    def test_files_are_read_as_text_only_for_known_text_suffixes_and_the_size_limit_matches_the_store(self, tmp_path):
        out = node(tmp_path, """
R.text = [h.isTextFile('Notes.MD'), h.isTextFile('a.py'), h.isTextFile('scan.pdf'), h.isTextFile('')];
R.problem = [h.fileProblem({ name: 'a.txt', size: 10 }), h.fileProblem({ name: 'big.bin', size: 6000000 }), h.fileProblem(null)]; R.max = h.FILE_MAX_BYTES;""")
        assert out["text"] == [True, True, False, False] and out["problem"][0] is None
        assert "limit is 5.0 MB" in out["problem"][1] and out["problem"][2] == "Pick a file."
        from dourmouse import projects_store as ps

        assert out["max"] == ps.FILE_MAX_BYTES
        h = (_DIR / "helpers.js").read_text(encoding="utf-8")
        listed = set(re.findall(r"'(\.[a-z0-9]+)'", h.split("TEXT_SUFFIXES = [")[1].split("]")[0]))
        assert listed == ps.TEXT_SUFFIXES

    def test_the_limits_match_the_store(self, tmp_path):
        from dourmouse import projects_store as ps

        out = node(tmp_path, "R.v = [h.NAME_MAX, h.GOAL_MAX, h.INSTRUCTIONS_MAX, h.NOTE_MAX, h.TITLE_MAX];")
        assert out["v"] == [ps.NAME_MAX, ps.GOAL_MAX, ps.INSTRUCTIONS_MAX, ps.NOTE_MAX, 60]


class TestWorkAndThreads:
    def test_the_checkbox_flips_done_and_a_running_item_reads_as_running(self, tmp_path):
        out = node(tmp_path, """
R.toggle = [h.workToggle({ status: 'todo' }), h.workToggle({ status: 'doing' }), h.workToggle({ status: 'done' })];
R.state = [h.workState({ status: 'doing' }), h.workState({ status: 'weird' }), h.workState(null)];
R.line = [h.workLine({ status: 'doing', job_id: 'j1', assignee: 'researcher' }), h.workLine({ status: 'todo', assignee: '' }), h.workLine({ status: 'doing', job_id: '' })];""")
        assert out["toggle"] == ["done", "done", "todo"] and out["state"] == ["doing", "todo", "todo"]
        assert out["line"] == ["Researcher, Running as a job", "", "In progress"]

    def test_stored_messages_become_finished_turns_and_an_unanswered_question_is_dropped(self, tmp_path):
        out = node(tmp_path, """
const m = [{ role: 'user', content: 'q1', at: 1760000000 }, { role: 'assistant', content: 'a1', at: 1760000001 },
  { role: 'user', content: 'q2', at: 1760000100 }, { role: 'assistant', content: 'a2', at: 1760000101 }, { role: 'user', content: 'dangling', at: 1760000200 }];
const r = h.turnRecords(m);
R.n = r.length; R.pairs = r.map((x) => [x.display_text, x.final_text, x.screen]); R.iso = /^20\\d\\d-/.test(r[0].timestamp);
R.junk = h.turnRecords(null).length; R.odd = h.turnRecords([{ role: 'assistant', content: 'x' }, { role: 'user', content: 'y' }]).length;""")
        assert out["n"] == 2 and out["pairs"] == [["q1", "a1", "PROJECTS"], ["q2", "a2", "PROJECTS"]] and out["iso"]
        assert out["junk"] == 0 and out["odd"] == 0

    def test_only_a_finished_answer_is_saved_to_a_thread(self, tmp_path):
        out = node(tmp_path, """
R.v = [h.answered({ status: 'done', reply: 'Hello' }), h.answered({ status: 'done', reply: 'ERROR: model failed' }), h.answered({ status: 'stopped', reply: 'half' }),
  h.answered({ status: 'error', reply: 'x' }), h.answered({ status: 'done', reply: '' }), h.answered(null), h.answered({ status: 'working', reply: 'so far' })];""")
        assert out["v"] == [True, False, False, False, False, False, False]


class TestSourceRules:
    def _src(self):
        return (_DIR / "index.js").read_text(encoding="utf-8"), (_DIR / "helpers.js").read_text(encoding="utf-8"), (_DIR / "projects.css").read_text(encoding="utf-8")

    def test_no_em_dash_gradient_uppercase_or_letter_spacing(self):
        js, helpers, css = self._src()
        for text in (js, helpers, css):
            assert "—" not in text
        assert "gradient(" not in css and "text-transform" not in css and "letter-spacing" not in css
        assert "innerHTML" not in js

    def test_css_uses_tokens_not_colour_literals(self):
        css = self._src()[2]
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css) and "rgb(" not in css and "rgba(" not in css

    def test_the_old_folder_history_screen_is_gone(self):
        js = self._src()[0]
        for old in ("/api/projects/open", "/api/projects/bookkeeper", "USE AS SCOPE", "Claude Code and Codex", "/api/os/projects/list"):
            assert old not in js

    def test_the_screen_has_its_own_thread_and_never_borrows_homes(self):
        reg = (_ROOT / "ui" / "assets" / "os" / "core" / "registry.js").read_text(encoding="utf-8")
        assert re.search(r"S\('PROJECTS', true,", reg)
        js = self._src()[0]
        assert "thread: true" in js and "restoreSource" in js and "start({ fresh: !resumed })" in js

    def test_a_thread_is_saved_only_as_an_answered_turn_through_the_project_route(self):
        js = self._src()[0]
        assert "/api/os/project/thread/turn" in js and "answered(t)" in js and "!t.restored" in js
        assert "/api/os/project/delete" in js and "confirm: true" in js and "confirmHere(" in js

    def test_thread_view_can_restore_from_a_source_and_be_destroyed(self):
        tv = (_KIT / "thread-view.js").read_text(encoding="utf-8")
        assert "restoreSource" in tv and "destroy()" in tv and "start({ fresh = false } = {})" in tv
        for upper in ("'THINKING'", "'COPY'", "'REGENERATE'"):
            assert upper not in tv


class TestRoutesTheScreenCalls:
    """The exact sequence the screen performs, against the real server and the real store."""

    @pytest.fixture
    def server(self, monkeypatch, tmp_path):
        import threading

        from dourmouse import projects_store as ps
        from dourmouse.general_roster import build_general_registry
        from dourmouse.goals import GoalStore, set_goal_store

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
        from dourmouse.webui import run_server

        set_goal_store(GoalStore(None))
        ps.set_project_store(ps.ProjectStore(tmp_path / "ws"))
        srv = run_server(build_general_registry(), port=0, client=None, config=None)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
        set_goal_store(None)
        ps.set_project_store(None)

    @staticmethod
    def call(srv, method, path, body=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request(method, path, body=json.dumps(body) if body is not None else None)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_create_thread_save_read_back_and_delete(self, server):
        call = self.call
        status, made = call(server, "POST", "/api/os/project/create", {"name": "Thesis", "template": "school"})
        assert status == 200
        pid = made["project"]["id"]
        _, got = call(server, "GET", f"/api/os/project/get?id={pid}")
        assert got["project"]["goal"] and got["project"]["agents"] and got["project"]["work_items"]
        _, th = call(server, "POST", "/api/os/project/thread/new", {"id": pid, "title": "New thread"})
        tid = th["thread"]["id"]
        call(server, "POST", "/api/os/project/thread/turn", {"id": pid, "thread": tid, "user": "What is due?", "assistant": "Nothing yet."})
        _, back = call(server, "GET", f"/api/os/project/thread?id={pid}&thread={tid}")
        assert [m["role"] for m in back["thread"]["messages"]] == ["user", "assistant"]
        assert back["thread"]["title"] == "What is due?"  # the first question names the thread
        _, listed = call(server, "GET", "/api/os/project/list")
        assert listed["projects"][0]["threads"] == 1
        assert call(server, "POST", "/api/os/project/delete", {"id": pid})[0] == 400  # the screen always sends confirm: true
        assert call(server, "POST", "/api/os/project/delete", {"id": pid, "confirm": True})[0] == 200
        assert call(server, "GET", f"/api/os/project/get?id={pid}")[0] == 404
