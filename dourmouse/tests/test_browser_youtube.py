"""Phase C2: browser_media controls the page's main video (YouTube's included) through its HTML5
media element: status, play, pause, seek, mute, unmute, volume. No YouTube API, no key.

* Pure tests: time parsing, the report wording, argument checks, a static check of the injected
  script (it never writes to the DOM).
* The REAL tool against a real Google Chrome (headless, launched by the agent) and a local page
  shaped like a YouTube watch page (``#movie_player.html5-video-player`` holding
  ``video.html5-main-video``, the title in ``h1.ytd-watch-metadata``), playing a short silent WAV
  generated here. Real YouTube was tried live in an isolated copy of the app; what happened there
  is recorded in the C2 finding, not assumed here.
"""

from __future__ import annotations

import http.server
import socketserver
import struct
import threading
import wave

import pytest

from dourmouse import browser_agent as ba
from dourmouse.tests.test_browser_element_ids import _eval, _reset_browser_state, needs_chrome

WATCH = """<!doctype html><html><head><title>(3) A Test Clip - YouTube</title></head><body>
<div id="movie_player" class="html5-video-player">
  <video class="html5-main-video" src="/clip.wav" preload="auto" style="width:480px;height:270px"></video>
</div>
<div id="title"><h1 class="style-scope ytd-watch-metadata"><yt-formatted-string>A Test Clip</yt-formatted-string></h1></div>
<video id="small" src="/clip.wav" style="width:40px;height:20px"></video>
</body></html>"""

PLAIN = """<!doctype html><title>Podcast page</title><h1>Episode 1</h1><audio id="a" src="/clip.wav" controls></audio>"""
NONE = """<!doctype html><title>No media here</title><p>Just text.</p>"""
# A player that keeps its own mute state and re-applies it to the element, as YouTube did live.
YTSIM = """<!doctype html><title>Sim - YouTube</title>
<div id="movie_player" class="html5-video-player"><video class="html5-main-video" src="/clip.wav" preload="auto"></video>
<div class="ytp-mute-button"><button class="ytp-volume-icon ytp-button" data-title-no-tooltip="Mute" aria-label="Mute (m)"></button></div></div>
<script>
const v = document.querySelector('video'), b = document.querySelector('.ytp-mute-button button');
window.__ytMuted = false;
b.addEventListener('click', () => { __ytMuted = !__ytMuted; b.setAttribute('data-title-no-tooltip', __ytMuted ? 'Unmute' : 'Mute'); b.setAttribute('aria-label', __ytMuted ? 'Unmute (m)' : 'Mute (m)'); v.muted = __ytMuted; });
v.addEventListener('volumechange', () => { if (v.muted !== __ytMuted) setTimeout(() => { v.muted = __ytMuted; }, 100); });
</script>"""
STUBBORN = """<!doctype html><title>Stubborn</title><video class="html5-main-video" src="/clip.wav" preload="auto"></video>
<script>const v = document.querySelector('video'); v.addEventListener('volumechange', () => setTimeout(() => { v.muted = false; v.volume = 1; }, 100));</script>"""
NOSEEK = """<!doctype html><title>Live-ish</title><video class="html5-main-video" src="/noseek.wav" preload="auto"></video>"""


def _wav(path, seconds=8, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * (seconds * rate))


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("c2media")
    for name, body in (("watch.html", WATCH), ("plain.html", PLAIN), ("none.html", NONE), ("noseek.html", NOSEEK), ("ytsim.html", YTSIM), ("stubborn.html", STUBBORN)):
        (root / name).write_text(body, encoding="utf-8")
    _wav(root / "clip.wav")

    class Handler(http.server.SimpleHTTPRequestHandler):
        """Serves byte ranges for the clip (a browser seeks in media through Range requests),
        except /noseek.wav, which is the same file from a server that does not."""

        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] == "/noseek.wav":
                self.path = "/clip.wav"
                return super().do_GET()
            rng = self.headers.get("Range")
            if not rng or not self.path.endswith(".wav"):
                return super().do_GET()
            data = (root / self.path.lstrip("/")).read_bytes()
            start_s, _, end_s = rng.replace("bytes=", "").partition("-")
            start = int(start_s or 0)
            end = int(end_s) if end_s else len(data) - 1
            chunk = data[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def chrome(site):
    mp = pytest.MonkeyPatch()
    mp.delenv("DOURMOUSE_ELECTRON_CDP_PORT", raising=False)
    mp.delenv("DOURMOUSE_ELECTRON_PANE_PORT", raising=False)
    mp.setenv("DOURMOUSE_BROWSER_HEADLESS", "1")
    # The agent's OWN headless Chrome aborts media requests for speed; a media test needs them.
    # (The shared pane no longer gets that filter at all: see the C2 finding.)
    mp.setenv("DOURMOUSE_BROWSER_BLOCK_MEDIA", "0")
    _reset_browser_state()
    ba._ensure_loop()
    yield
    _reset_browser_state()
    mp.undo()


def _ready():
    ba._call(lambda: ba._PAGE.wait_for_function("document.querySelector('video.html5-main-video, audio') && document.querySelector('video.html5-main-video, audio').readyState >= 1", timeout=10_000))


class TestPure:
    @pytest.mark.parametrize(("value", "seconds"), [(83, 83.0), ("83.5", 83.5), ("1:23", 83.0), ("01:02:03", 3723.0), ("0:05.5", 5.5), (" 2:00 ", 120.0)])
    def test_times(self, value, seconds):
        assert ba._seconds(value) == seconds

    @pytest.mark.parametrize("value", [None, True, "soon", "1:2:3:4", "", "1:75x"])
    def test_not_times(self, value):
        assert ba._seconds(value) is None

    def test_clock(self):
        assert ba._clock(83.4) == "1:23" and ba._clock(3723) == "1:02:03" and ba._clock(None) == "unknown"

    @pytest.mark.parametrize(
        ("args", "needle"),
        [
            ({"action": "rewind"}, "must be one of"),
            ({"action": "seek"}, "needs 'to'"),
            ({"action": "seek", "to": "later"}, "needs 'to'"),
            ({"action": "seek", "by": "a bit"}, "'by' must be a number"),
            ({"action": "volume"}, "needs 'level'"),
            ({"action": "volume", "level": 140}, "from 0 to 100"),
        ],
    )
    def test_bad_arguments_are_refused_before_anything_runs(self, args, needle):
        out = ba.browser_media(args)
        assert out.startswith("ERROR") and needle in out

    def test_report_for_an_advert_and_no_media(self):
        st = {"found": True, "youtube": True, "kind": "video", "title": "T", "currentTime": 3.2, "duration": 15.0, "paused": False, "muted": True, "volume": 1, "ad": True, "url": "u", "count": 1}
        out = ba._media_report(st, "PLAYING.")
        assert out.startswith("PLAYING.\nMEDIA (YouTube video): 'T'") and "advert" in out and "muted" in out
        assert ba._media_report({"found": False, "url": "https://x/"}, "").startswith("NO MEDIA")

    def test_the_media_script_writes_nothing_to_the_dom_and_has_one_hidden_global(self):
        src = ba._MEDIA_SCRIPT_PATH.read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("//"))
        for forbidden in ("setAttribute(", "removeAttribute(", ".dataset", "classList.add", "classList.remove", "innerHTML", "appendChild(", "document.write", "window."):
            assert forbidden not in code, forbidden
        assert "enumerable: false, configurable: false, writable: false" in src
        assert "—" not in src


@needs_chrome
class TestRealMediaElement:
    def test_status_reads_the_youtube_title_time_and_state(self, chrome, site):
        ba.browser_open({"url": site + "/watch.html"})
        _ready()
        out = ba.browser_media({"action": "status"})
        assert out.startswith("MEDIA (YouTube video): 'A Test Clip'")
        assert "TIME: 0:00 of 0:08" in out and "STATE: paused" in out
        assert "2 media elements on the page" in out

    def test_mute_play_pause_seek_volume_unmute(self, chrome, site):
        ba.browser_open({"url": site + "/watch.html"})
        _ready()
        assert ba.browser_media({"action": "mute"}).startswith("MUTED.")
        assert _eval("document.querySelector('video.html5-main-video').muted") is True
        played = ba.browser_media({"action": "play"})
        assert played.startswith("PLAYING."), played
        ba.browser_wait({"ms": 700})
        t = _eval("document.querySelector('video.html5-main-video').currentTime")
        assert t > 0.2  # the media clock really ran
        assert ba.browser_media({"action": "pause"}).startswith("PAUSED.")
        assert _eval("document.querySelector('video.html5-main-video').paused") is True
        sought = ba.browser_media({"action": "seek", "to": "0:05"})
        assert sought.startswith("SOUGHT to 0:05")
        assert abs(_eval("document.querySelector('video.html5-main-video').currentTime") - 5.0) < 0.1
        back = ba.browser_media({"action": "seek", "by": -2})
        assert back.startswith("SOUGHT to 0:03")
        beyond = ba.browser_media({"action": "seek", "to": 999})
        assert "0:07" in beyond.splitlines()[0]  # clamped to the end, not past it
        assert ba.browser_media({"action": "volume", "level": 30}).startswith("VOLUME set to 30%")
        assert abs(_eval("document.querySelector('video.html5-main-video').volume") - 0.3) < 0.01
        assert ba.browser_media({"action": "unmute"}).startswith("UNMUTED.")
        assert _eval("document.querySelector('video.html5-main-video').muted") is False
        # the small second video was never touched
        assert _eval("document.getElementById('small').paused") is True and _eval("document.getElementById('small').volume") == 1

    def test_an_audio_element_on_an_ordinary_page(self, chrome, site):
        ba.browser_open({"url": site + "/plain.html"})
        _ready()
        out = ba.browser_media({"action": "status"})
        assert out.startswith("MEDIA (audio): 'Podcast page'")

    def test_a_seek_the_server_does_not_allow_is_reported_as_not_done(self, chrome, site):
        ba.browser_open({"url": site + "/noseek.html"})
        _ready()
        out = ba.browser_media({"action": "seek", "to": 5})
        assert out.startswith("ASKED TO SEEK to 0:05, but it is at 0:00"), out
        assert "PROBLEM: the player did not move there" in out

    def test_a_player_with_its_own_mute_state_is_muted_through_its_own_button(self, chrome, site):
        ba.browser_open({"url": site + "/ytsim.html"})
        _ready()
        out = ba.browser_media({"action": "mute"})
        assert out.startswith("MUTED."), out
        assert _eval("window.__ytMuted") is True  # the player's own state, so it sticks
        assert "YouTube's own mute button" not in out  # the player and the element agree
        ba.browser_wait({"ms": 400})
        assert _eval("document.querySelector('video').muted") is True
        assert ba.browser_media({"action": "unmute"}).startswith("UNMUTED.")
        assert _eval("window.__ytMuted") is False

    def test_a_player_that_undoes_the_change_is_reported_not_claimed(self, chrome, site):
        ba.browser_open({"url": site + "/stubborn.html"})
        _ready()
        out = ba.browser_media({"action": "mute"})
        assert out.startswith("ASKED TO MUTE, but it is not muted"), out
        assert "PROBLEM: the page's player changed it back" in out
        vol = ba.browser_media({"action": "volume", "level": 20})
        assert vol.startswith("ASKED FOR VOLUME 20%, but it is 100%"), vol

    def test_no_media(self, chrome, site):
        ba.browser_open({"url": site + "/none.html"})
        assert ba.browser_media({"action": "status"}).startswith("NO MEDIA")
        assert ba.browser_media({"action": "play"}).startswith("NO MEDIA")


class TestLiveCheckHelper:
    """scripts/live_checks/run_docs_youtube_check.py: it refuses the owner's app unless told, and
    talks only to the two ports it is given."""

    @staticmethod
    def _load():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "live_checks" / "run_docs_youtube_check.py"
        spec = importlib.util.spec_from_file_location("run_docs_youtube_check", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @pytest.mark.parametrize("ports", [["--cdp-port", "9333", "--pane-port", "19334"], ["--cdp-port", "19333", "--pane-port", "9334"], ["--cdp-port", "1", "--pane-port", "2", "--ui-port", "8765"]])
    def test_the_owners_ports_are_refused_without_owner_app(self, ports, capsys):
        mod = self._load()
        assert mod.main([*ports, "--youtube-url", "https://www.youtube.com/watch?v=x"]) == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_nothing_to_do_and_a_dead_bridge(self, capsys):
        import socket

        mod = self._load()
        assert mod.main(["--cdp-port", "19333", "--pane-port", "19334"]) == 2
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert mod.main(["--cdp-port", "19333", "--pane-port", str(free), "--youtube-url", "https://www.youtube.com/watch?v=x"]) == 1
        assert "did not answer" in capsys.readouterr().err

    def test_it_never_signs_in_or_closes_the_apps_browser(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "scripts" / "live_checks" / "run_docs_youtube_check.py").read_text(encoding="utf-8")
        for banned in ("browser_signin", "browser_creds", "close_browser(", "browser_submit", "password", '"action": "unmute"'):
            assert banned not in src.replace("never signs in", "").replace("types a password", ""), banned
        assert "—" not in src
