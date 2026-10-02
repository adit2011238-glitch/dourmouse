"""Phase D: MEDIA's queue, control-event, highlight-geometry and web-link helpers
(run in node) and the source rules for the new player and reader code."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "media"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_queue_navigation_has_no_wraparound(tmp_path):
    out = node(tmp_path, """
const q = [{ path: '/a' }, { path: '/b' }, { path: '/c' }];
R.next = [h.neighbour(q, '/a', 1), h.neighbour(q, '/b', 1), h.neighbour(q, '/c', 1)];
R.prev = [h.neighbour(q, '/a', -1), h.neighbour(q, '/b', -1), h.neighbour(q, '/c', -1)];
R.unqueued = [h.neighbour(q, '/z', 1), h.neighbour(q, '/z', -1)];
R.empty = [h.neighbour([], '/a', 1), h.neighbour(null, '/a', 1)];
R.move = [h.reorderTarget(0, -1, 3), h.reorderTarget(0, 1, 3), h.reorderTarget(2, 1, 3), h.reorderTarget(2, -1, 3), h.reorderTarget(5, 1, 3)];
R.label = [h.queueLabel(q, '/b'), h.queueLabel(q, '/z'), h.queueLabel(null, '/b')];
""")
    assert out["next"] == [1, 2, -1] and out["prev"] == [-1, 0, 1]
    assert out["unqueued"] == [0, -1] and out["empty"] == [-1, -1]
    assert out["move"] == [-1, 1, -1, 1, -1]
    assert out["label"] == ["2 of 3", "", ""]


def test_seek_target_clamps_and_survives_unknown_duration(tmp_path):
    out = node(tmp_path, """
R.a = [h.seekTarget(10, 5, 100), h.seekTarget(98, 5, 100), h.seekTarget(2, -5, 100), h.seekTarget(10, 5, NaN), h.seekTarget(NaN, 5, 100), h.seekTarget(10, 5, 0)];
""")
    assert out["a"] == [15, 100, 0, 15, 5, 15]


def test_control_plan_matches_what_the_server_broadcasts(tmp_path):
    out = node(tmp_path, """
const cur = '/Users/me/Movies/a.mp4';
R.seek = h.controlPlan({ type: 'player_control', action: 'seek', seconds: 42, path: cur }, cur);
R.play = h.controlPlan({ action: 'PLAY', path: cur }, cur);
R.other = h.controlPlan({ action: 'pause', path: '/Users/me/Movies/b.mp4' }, cur);
R.alias = h.controlPlan({ action: 'play', path: '~/Movies/a.mp4' }, cur, { '~/Movies/a.mp4': cur });
R.nopath = h.controlPlan({ action: 'pause' }, cur);
R.bad = [h.controlPlan(null, cur), h.controlPlan({ action: 'explode' }, cur), h.controlPlan({ action: 'seek', seconds: -1 }, cur), h.controlPlan({ action: 'seek' }, cur), h.controlPlan({ action: 'seek', seconds: 'x' }, cur)];
""")
    assert out["seek"] == {"action": "seek", "seconds": 42, "reopen": False, "path": "/Users/me/Movies/a.mp4"}
    assert out["play"]["action"] == "play" and out["play"]["reopen"] is False
    assert out["other"]["reopen"] is True, "a different file must be opened first"
    assert out["alias"]["reopen"] is False, "a ~ spelling of the open file must not reload it and lose the position"
    assert out["nopath"]["reopen"] is False
    assert out["bad"] == [None] * 5


def test_highlight_rect_from_a_drag_is_page_relative_and_clipped(tmp_path):
    out = node(tmp_path, """
const box = { left: 100, top: 50, width: 200, height: 400 };
R.a = h.rectFromDrag({ x: 120, y: 90 }, { x: 220, y: 130 }, box);
R.reversed = h.rectFromDrag({ x: 220, y: 130 }, { x: 120, y: 90 }, box);
R.clipped = h.rectFromDrag({ x: 0, y: 0 }, { x: 1000, y: 1000 }, box);
R.click = h.rectFromDrag({ x: 120, y: 90 }, { x: 121, y: 90 }, box);
R.nobox = h.rectFromDrag({ x: 1, y: 1 }, { x: 9, y: 9 }, { left: 0, top: 0, width: 0, height: 0 });
R.page = [h.pageClamp(-3, 5), h.pageClamp(9, 5), h.pageClamp('2', 5), h.pageClamp(1, 0), h.pageClamp(NaN, 5)];
R.onpage = h.highlightsOnPage([{ page: 0 }, { page: 1 }, { page: 1 }, null], 1).length;
""")
    assert out["a"] == {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1}
    assert out["reversed"] == out["a"]
    assert out["clipped"] == {"x": 0, "y": 0, "w": 1, "h": 1}
    assert out["click"] is None and out["nobox"] is None
    assert out["page"] == [0, 4, 2, 0, 0] and out["onpage"] == 2


def test_web_links_only_open_the_two_services(tmp_path):
    out = node(tmp_path, """
const u = (t, s) => h.parseWebLink(t, s);
R.yt = u('https://www.youtube.com/watch?v=abc', 'youtube');
R.short = u('https://youtu.be/abc', 'youtube');
R.music = u('https://music.youtube.com/watch?v=abc', 'youtube');
R.sp = u('https://open.spotify.com/track/xyz', 'spotify');
R.search = [u('lofi beats', 'youtube'), u('a b&c', 'spotify')];
R.home = [u('', 'youtube'), u('  ', 'spotify')];
R.bad = [
  u('http://www.youtube.com/watch?v=1', 'youtube'),
  u('https://evil.example/youtube.com', 'youtube'),
  u('https://www.youtube.com.evil.example/', 'youtube'),
  u('https://open.spotify.com/x', 'youtube'),
  u('javascript:alert(1)', 'youtube'),
  u('file:///etc/passwd', 'spotify'),
  u('//evil.example/x', 'youtube'),
  u('https://youtube.com@evil.example/', 'youtube'),
  u('x', 'tiktok'),
];
""")
    assert out["yt"]["url"] == "https://www.youtube.com/watch?v=abc" and out["yt"]["how"] == "link"
    assert out["short"]["ok"] and out["music"]["ok"] and out["sp"]["ok"]
    assert out["search"][0]["url"] == "https://www.youtube.com/results?search_query=lofi%20beats"
    assert out["search"][1]["url"] == "https://open.spotify.com/search/a%20b%26c"
    assert out["home"][0]["url"] == "https://www.youtube.com/" and out["home"][1]["url"] == "https://open.spotify.com/"
    assert all(r["ok"] is False and r["error"] for r in out["bad"]), out["bad"]


class TestSource:
    def _src(self):
        return {p.name: re.sub(r"/\*.*?\*/", "", p.read_text(encoding="utf-8"), flags=re.S) for p in _DIR.rglob("*") if p.suffix in {".js", ".css"}}

    def test_new_controls_have_spec_sentences(self):
        text = self._src()["index.js"]
        for control in ("prevBtn", "nextBtn", "muteBtn", "vol", "queueAddBtn", "queueClear", "webIn", "ytBtn", "spBtn"):
            assert re.search(rf"spec\({control},", text), control
        pdf = self._src()["pdf-view.js"]
        assert pdf.count("dataset.spec") >= 6

    def test_no_raw_timers_and_no_typed_urls_in_any_screen_file(self):
        for name, text in self._src().items():
            if name.endswith(".js"):
                assert not re.search(r"\bsetTimeout\s*\(|\bsetInterval\s*\(", text), name
                assert "/api/files/" not in text, f"{name}: URLs must come from the open route"
                assert "innerHTML" not in text, name

    def test_the_screen_listens_for_player_control_and_reports_back(self):
        text = self._src()["index.js"]
        assert "ctx.events.on('player_control'" in text
        assert "/api/os/media/player-state" in text and "ctx.every(REPORT_MS" in text

    def test_media_keys_are_bound_and_scoped(self):
        text = self._src()["index.js"]
        for combo in ("'ArrowRight'", "'ArrowLeft'", "'ArrowUp'", "'ArrowDown'", "'m'", "'Shift+ArrowRight'"):
            assert f"ctx.keys.bind({combo}" in text, combo
        assert "root.addEventListener('keydown'" in text and "document.addEventListener" not in text

    def test_css_is_scoped_to_the_screen(self):
        for line in self._src()["media.css"].splitlines():
            if line.strip() and "{" in line and not line.startswith(" "):
                assert line.startswith('[data-screen="MEDIA"]'), line
