"""Finding #152: VOICE's pure helpers (node) and the source rules for the screen."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "voice"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestPlan:
    def test_open_panel_maps_to_os_screens_and_refuses_what_has_none(self, tmp_path):
        out = node(tmp_path, """
const open = (panel) => h.planFor('open ' + panel, { recognized: true, command: { action: 'open_panel', args: { panel } } });
R.mail = open('mail'); R.map = open('map'); R.chat = open('chat'); R.research = open('research');
R.globe = open('globe'); R.d3 = open('design3d'); R.odd = open('nonsense');
""")
        assert (out["mail"]["kind"], out["mail"]["slug"]) == ("navigate", "comms")
        assert out["map"]["slug"] == "atlas" and out["chat"]["slug"] == "home" and out["research"]["slug"] == "research"
        assert out["globe"]["kind"] == "refuse" and "classic console" in out["globe"]["say"]
        assert out["d3"]["kind"] == "refuse"
        assert out["odd"]["kind"] == "refuse"

    def test_search_email_close_and_unrecognised(self, tmp_path):
        out = node(tmp_path, """
const cmd = (action, args) => ({ recognized: true, command: { action, args } });
R.search = h.planFor('search for nvidia earnings', cmd('search', { query: 'nvidia earnings' }));
R.email = h.planFor('email sam saying late', cmd('email', { person: 'sam', message: 'running late' }));
R.close = h.planFor('close research', cmd('close_panel', { panel: 'research' }));
R.free = h.planFor('  what is the time ', { ok: true, recognized: false });
R.nul = h.planFor('x', null);
R.unknown = h.planFor('x', cmd('teleport', {}));
""")
        assert out["search"] == {"kind": "chat", "text": "search for nvidia earnings", "say": 'Sending "search for nvidia earnings" to the companion on HOME.'}
        assert out["email"]["text"] == "email sam: running late" and "until you approve" in out["email"]["say"]
        assert out["close"]["kind"] == "refuse"
        assert out["free"]["kind"] == "chat" and out["free"]["text"] == "what is the time"
        assert out["nul"]["kind"] == "chat"
        assert out["unknown"]["kind"] == "refuse" and "teleport" in out["unknown"]["say"]


class TestEngine:
    def test_engine_choice_is_honest_about_what_is_configured(self, tmp_path):
        out = node(tmp_path, """
const on = { stt: 'faster-whisper', whisper_model: 'base' }, off = { stt: 'not-configured' };
R.server = h.chooseEngine(on, { mic: true, speechRecognition: true });
R.browser = h.chooseEngine(off, { mic: true, speechRecognition: true });
R.serverNoMic = h.chooseEngine(on, { mic: false, speechRecognition: true });
R.none = h.chooseEngine(off, { mic: true, speechRecognition: false });
R.nullVoice = h.chooseEngine(null, { mic: false, speechRecognition: false });
R.wake = [h.wakewordTag(null), h.wakewordTag({ enabled: false }), h.wakewordTag({ enabled: true, inference_engine: 'not-configured', capture_engine: 'installed' }), h.wakewordTag({ enabled: true, inference_engine: 'installed', capture_engine: 'installed' })];
R.b64 = [h.stripDataUrl('data:audio/webm;base64,QUJD'), h.stripDataUrl('nope'), h.stripDataUrl('')];
""")
        assert out["server"]["id"] == "server" and "base" in out["server"]["label"]
        assert out["browser"]["id"] == "browser" and "not configured" in out["browser"]["note"]
        assert out["serverNoMic"]["id"] == "browser"
        assert out["none"]["ok"] is False and "Typed commands still work" in out["none"]["note"]
        assert out["nullVoice"]["id"] == "none"
        assert [w["text"] for w in out["wake"]] == ["unknown", "off", "on, missing parts", "enabled"]
        assert out["b64"] == ["QUJD", "", ""]


class TestSource:
    def _src(self):
        return {p.name: p.read_text(encoding="utf-8") for p in _DIR.rglob("*") if p.suffix in {".js", ".css"}}

    def test_no_mockup_sample_text_is_left_and_wakeword_is_not_hardcoded(self):
        text = self._src()["index.js"]
        assert "not configured" not in text, "the wakeword tag must come from the server read"
        assert "open mail" not in text and "running late" not in text

    def test_every_utterance_goes_through_the_servers_parser(self):
        text = self._src()["index.js"]
        assert "/api/voice/command" in text and "/api/os/voice/info" in text and "/api/voice'" in text
        assert not re.search(r"\bfetch\s*\(", text)

    def test_the_recording_is_bounded_and_the_microphone_is_released(self):
        text = self._src()["index.js"]
        assert "MAX_RECORD_S" in text and "getTracks().forEach((t) => t.stop())" in text
        assert "ctx.every(1000" in text

    def test_every_control_has_a_spec_sentence(self):
        text = self._src()["index.js"]
        assert text.count("data-spec=") >= 2 and "spec: 'Starts capture" in text
