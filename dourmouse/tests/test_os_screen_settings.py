"""Finding #150: the SETTINGS screen's pure helpers (node) and its source rules.

The DOM code is verified live in a real browser (EVIDENCE/149_os_settings.png).
The routes are tested in test_os_api_settings.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "settings"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


FEATURES = """[
 { key: 'A', section: 'security', kind: 'bool', default: true, value: false, label: 'Scans', help: 'Scan it.' },
 { key: 'B', section: 'security', kind: 'bool', default: true, value: true, label: 'Watch', help: 'Watch it.' },
 { key: 'C', section: 'agents', kind: 'bool', default: true, value: true, label: 'Agents', help: 'Run.' },
 { key: 'D', section: 'agents', kind: 'paths', default: '', value: [], label: 'Folders', help: 'Where.' },
]"""


class TestHelpers:
    def test_switches_are_grouped_by_section_and_folders_are_kept_apart(self, tmp_path):
        out = node(tmp_path, f"""
const g = h.groupFeatures({FEATURES});
R.titles = g.map((x) => x.title);
R.sw = g.map((x) => x.switches.map((s) => s.key));
R.fo = g.map((x) => x.folders.map((s) => s.key));
R.junk = h.groupFeatures(null);
""")
        assert out["titles"] == ["Security", "Agents"] and out["sw"] == [["A", "B"], ["C"]] and out["fo"] == [[], ["D"]] and out["junk"] == []

    def test_turning_a_security_switch_off_says_it_lowers_protection(self, tmp_path):
        out = node(tmp_path, f"""
const f = {FEATURES};
R.off = h.featurePrompt(f[1], false);
R.on = h.featurePrompt(f[0], true);
R.agent = h.featurePrompt(f[2], false);
""")
        assert "protection stops at the next launch" in out["off"] and "next time Dourmouse starts" in out["off"]
        assert "protection stops" not in out["on"] and "protection stops" not in out["agent"]

    def test_the_auto_approve_card_states_what_it_does(self, tmp_path):
        out = node(tmp_path, """
const t = { id: 'auto_approve', label: 'Auto approve', help: 'x' };
R.on = h.togglePrompt(t, true);
R.off = h.togglePrompt(t, false);
R.other = h.togglePrompt({ id: 'grounded_mode', label: 'Grounded mode', help: 'Ask once.' }, true);
""")
        assert "without asking you" in out["on"] and "send mail" in out["on"] and "every tab" in out["on"]
        assert "come back" in out["off"]
        assert "Grounded mode" in out["other"] and "send mail" not in out["other"]

    def test_reset_names_what_changes_and_what_it_leaves_alone(self, tmp_path):
        out = node(tmp_path, f"""
const f = {FEATURES};
R.some = h.resetPrompt(f);
R.none = h.resetPrompt([f[1], f[2]]);
R.differs = f.map((x) => h.differsFromDefault(x));
""")
        assert "Scans" in out["some"] and "Watch" not in out["some"].split("does not touch")[0].split("differ now")[1]
        assert "API keys" in out["some"] and "auto approve" in out["some"]
        assert "already match" in out["none"]
        assert out["differs"] == [True, False, False, False]

    def test_shell_choices_disable_electron_only_when_it_is_missing(self, tmp_path):
        out = node(tmp_path, """
R.have = h.shellChoices({ requested: 'auto', electron_available: true });
R.lack = h.shellChoices({ requested: 'pywebview', electron_available: false });
R.p = h.shellPrompt('auto', { electron_available: false });
""")
        assert [c["selected"] for c in out["have"]] == [True, False, False] and not any(c["disabled"] for c in out["have"])
        el = next(c for c in out["lack"] if c["value"] == "electron")
        assert el["disabled"] and "npm install" in el["reason"]
        assert "pywebview" in out["p"] and "next starts" in out["p"]

    def test_key_and_token_wording(self, tmp_path):
        out = node(tmp_path, """
R.saved = h.keyTag('saved');
R.none = h.keyTag('none');
R.odd = h.keyTag('???');
R.tok = h.tokenTag({ token_gate: true, loopback: true });
R.open_local = h.tokenTag({ token_gate: false, loopback: true });
R.open_lan = h.tokenTag({ token_gate: false, loopback: false });
R.v6 = h.bindLine({ host: '::1', port: 9 });
R.v4 = h.bindLine({ host: '127.0.0.1', port: 8765 });
""")
        assert out["saved"]["tone"] == "ok" and out["none"]["tone"] == "warn" and out["odd"]["word"] == "unknown"
        assert out["tok"]["word"] == "set" and out["open_local"]["tone"] == "" and out["open_lan"]["tone"] == "bad"
        assert out["v6"] == "[::1]:9" and out["v4"] == "127.0.0.1:8765"

    def test_local_models_are_a_policy_not_a_claimed_block(self, tmp_path):
        out = node(tmp_path, """
R.policy = h.localModelRow({ local_active: false });
R.live = h.localModelRow({ local_active: true, base_url: 'http://127.0.0.1:11434' });
""")
        assert out["policy"]["word"] == "policy" and "not a lock" in out["policy"]["text"]
        assert out["live"]["tone"] == "bad" and "127.0.0.1" in out["live"]["text"]

    def test_backend_rows_report_ready_or_the_real_reason(self, tmp_path):
        out = node(tmp_path, """
R.ok = h.backendRow({ name: 'ollama', configured: true, model: 'm', detail: 'local server answered' });
R.no = h.backendRow({ name: 'nvidia', configured: false, model: 'x', detail: 'NVIDIA_API_KEY not set' });
R.junk = h.backendRow(null);
""")
        assert out["ok"]["word"] == "ready" and out["no"]["word"] == "not ready" and out["no"]["detail"] == "NVIDIA_API_KEY not set"
        assert out["junk"]["name"] == ""

    def test_reduced_motion_reads_the_os_or_says_unknown(self, tmp_path):
        out = node(tmp_path, """
R.yes = h.osReducedMotion({ matchMedia: () => ({ matches: true }) });
R.no = h.osReducedMotion({ matchMedia: () => ({ matches: false }) });
R.none = h.osReducedMotion({});
R.boom = h.osReducedMotion({ matchMedia: () => { throw new Error('x'); } });
""")
        assert out["yes"] is True and out["no"] is False and out["none"] is None and out["boom"] is None

    def test_folder_lines_are_trimmed_and_bounded(self, tmp_path):
        out = node(tmp_path, """
R.a = h.parseFolders(' /a \\n\\n/b\\n');
R.big = h.parseFolders('/x\\n'.repeat(500)).length;
""")
        assert out["a"] == ["/a", "/b"] and out["big"] == 50


class TestSource:
    def test_no_mockup_sample_data_is_left_in_the_screen(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in _DIR.glob("*.js"))
        for sample in ("claude-sonnet-5", "gpt-oss:120b", "disabled by policy"):
            assert sample not in text

    def test_every_control_has_a_spec_sentence(self):
        text = (_DIR / "index.js").read_text(encoding="utf-8")
        buttons = re.findall(r"<button\b[^>]*>", text)
        assert buttons and all("data-spec" in b for b in buttons)

    def test_every_write_asks_first(self):
        text = (_DIR / "index.js").read_text(encoding="utf-8")
        writes = [m.start() for m in re.finditer(r"\.post\(", text)]
        assert writes
        for pos in writes:
            before = text[max(0, pos - 700):pos]
            assert "ask(" in before or "confirmHere(" in before, text[pos - 120:pos + 80]
