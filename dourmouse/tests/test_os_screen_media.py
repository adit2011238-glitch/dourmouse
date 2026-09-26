"""Finding #152: MEDIA's pure helpers (node) and the source rules for the screen."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "media"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHelpers:
    def test_clock_formats_and_refuses_bad_numbers(self, tmp_path):
        out = node(tmp_path, """
R.a = [h.clock(68.4), h.clock(0), h.clock(178), h.clock(3725)];
R.bad = [h.clock(NaN), h.clock(-1), h.clock(Infinity), h.clock('3'), h.clock(undefined)];
""")
        assert out["a"] == ["1:08", "0:00", "2:58", "1:02:05"]
        assert out["bad"] == ["", "", "", "", ""]

    def test_info_line_shows_only_what_was_read(self, tmp_path):
        out = node(tmp_path, """
const row = { name: 'a.mp4', size: 1536 };
R.none = h.infoLine(row, {});
R.full = h.infoLine(row, { duration: 178, video: ['h264'], width: 194, height: 134 });
R.audio = h.infoLine({ name: 'x.flac', size: 3 * 1024 * 1024 }, { duration: 61, audio: ['flac'] });
R.empty = h.infoLine(null, {});
""")
        assert out["none"] == "a.mp4 · 1.5 KB", "no codec, duration or size that was not read"
        assert out["full"] == "a.mp4 · 2:58 · H264 · 194x134 · 1.5 KB"
        assert out["audio"] == "x.flac · 1:01 · FLAC · 3.0 MB"
        assert out["empty"] == ""

    def test_convert_label_and_errors_and_fraction(self, tmp_path):
        out = node(tmp_path, """
R.a = h.convertLabel({ route: 'transcode', progress: 0.424 });
R.b = h.convertLabel({ route: 'remux' });
R.c = h.convertLabel(null);
R.err = [1, 2, 3, 4, 9].map(h.mediaErrorText);
R.frac = [h.fraction(30, 60), h.fraction(90, 60), h.fraction(5, 0), h.fraction(-1, 10)];
R.size = [h.sizeLabel(0), h.sizeLabel(1023), h.sizeLabel(1048576), h.sizeLabel(-5), h.sizeLabel('x')];
R.fmt = [h.formatText(['mp3', 'wav']), h.formatText([]), h.formatText(null)];
""")
        assert out["a"] == "Converting the file for playback: 42%."
        assert out["b"] == "Repackaging the file for playback."
        assert out["c"] == "Preparing the file for playback."
        assert out["err"][2].startswith("The file was found") and out["err"][4] == "This window could not play the file."
        assert out["frac"] == [0.5, 1, 0, 0]
        assert out["size"] == ["0 B", "1023 B", "1.0 MB", "", ""]
        assert out["fmt"] == ["mp3 wav", "none", "none"]


class TestSource:
    def _src(self):
        return {p.name: re.sub(r"/\*.*?\*/", "", p.read_text(encoding="utf-8"), flags=re.S) for p in _DIR.rglob("*") if p.suffix in {".js", ".css"}}

    def test_no_sample_data_from_the_mockup_is_left(self):
        for name, text in self._src().items():
            for sample in ("verification.mp4", "194x134 ·", "1:08", "2:58", "38%"):
                assert sample not in text, f"{name} still carries the mockup sample {sample!r}"

    def test_the_screen_never_builds_a_media_url_from_typed_text(self):
        text = self._src()["index.js"]
        assert "/api/files/" not in text, "URLs must come from POST /api/os/media/open"
        assert "urls.media" in text and "urls.status" in text and "urls.image" in text

    def test_the_conversion_poll_is_bounded_and_goes_through_ctx(self):
        text = self._src()["index.js"]
        assert "ctx.every(CONVERT_POLL_MS" in text and "CONVERT_POLL_MAX" in text
        assert not re.search(r"\bsetTimeout\s*\(|\bsetInterval\s*\(", text)

    def test_every_control_has_a_spec_sentence(self):
        text = self._src()["index.js"]
        for control in ("pathIn", "goBtn", "playBtn", "seek"):
            assert re.search(rf"spec\({control},", text), control
        assert "spec: 'Shows the path field" in text
