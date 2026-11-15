"""Finding 205 (plan 1.9): Research, Security, Library (wiki, mail, media), Browser and Settings follow the shell's style rules.

The screens join the shell's own style check (scripts/ui_style_lint.mjs): no gradients, uppercase, letter spacing,
colour literals, decorative shadows or removed focus outlines in their CSS, and no em dash or emoji in their text.
Button labels are sentence case. The Security helpers that word the status page are pinned too, because a helper
that turned a collector that did not answer into "Protected" would be a fake all-clear.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCREENS = _ROOT / "ui" / "assets" / "os" / "screens"
_NODE = shutil.which("node")
FOLDERS = ("security", "research", "settings", "browser", "wiki", "comms", "media")


def _files(suffix):
    return [f for d in FOLDERS for f in sorted((_SCREENS / d).glob("*" + suffix))]


def _node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "s.mjs"
    script.write_text(body, encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_five_places_pass_the_shells_style_lint(tmp_path):
    lint = (_ROOT / "scripts" / "ui_style_lint.mjs").as_uri()
    css = json.dumps([str(f) for f in _files(".css")])
    js = json.dumps([str(f) for f in _files(".js")])
    out = _node(tmp_path, f"""import {{ readFileSync }} from 'node:fs';
const m = await import({lint!r});
const found = [];
for (const f of {css}) found.push(...m.lintCss(readFileSync(f, 'utf8'), f));
for (const f of {js}) found.push(...m.lintText(readFileSync(f, 'utf8'), f));
console.log(JSON.stringify(found.map((x) => x.at + ' ' + x.rule)));""")
    assert out == []


# quoted all-capital words that are codes or names, not words a person reads on a control
_CODES = {"ALL", "MISSING", "UNSUMMARIZED", "SUMMARIZED", "BLOCKED", "REJECTED", "EXECUTING", "READY", "CREATED", "VERIFYING", "COMPLETED",
          "OFF", "MLA", "APA", "KB", "MB", "GB", "TB", "WIKI", "RESEARCH", "MEDIA", "COMMS", "SETTINGS", "SECURITY", "BROWSER", "HOME",
          "TEXTAREA", "INPUT", "BUTTON", "SELECT", "HIGHLIGHT", "RECENT", "UTC", "GET", "POST", "PUT", "DELETE_"}


@pytest.mark.parametrize("path", _files(".js"), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_control_labels_are_sentence_case(path):
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r"""(?:label:|button\(|'os-btn[^']*',)\s*'([^']+)'""", text):
        word = m.group(1)
        letters = re.sub(r"[^A-Za-z]", "", word)
        assert not (len(letters) >= 3 and letters.isupper() and word not in _CODES), f"{path.name}: {word!r} is all capitals"
    for m in re.finditer(r"\b(?:Press|press|Use|use) ([A-Z]{3,}(?: [A-Z]{2,})*)\b", text):
        assert m.group(1) in _CODES, f"{path.name}: prose names a control in capitals: {m.group(1)!r}"


def test_security_status_helpers_never_turn_a_missing_read_into_protected(tmp_path):
    helpers = (_SCREENS / "security" / "helpers.js").as_uri()
    out = _node(tmp_path, f"""import * as h from {helpers!r};
const R = {{}};
const d = {{ scanned: true, last_scan_at: 1790429334, findings_by_severity: {{ high: 1, med: 1, low: 0 }}, findings: [],
  posture: [{{ dimension: 'a', label: 'A', rating: 'at_risk', evidence: [{{}}], not_checked: [] }},
            {{ dimension: 'b', label: 'B', rating: 'unknown', evidence: [], not_checked: ['dns', 'downloads_assessed'] }},
            {{ dimension: 'c', label: 'C', rating: 'good', evidence: [], not_checked: [] }}] }};
R.rows = h.areaRows(d).map((r) => [r.label, r.word, r.tone, r.note]);
R.top = h.headline(d);
R.none = h.headline({{ scanned: false }}).text; R.nul = h.headline(null).text;
R.clean = h.headline({{ scanned: true, findings_by_severity: {{ high: 0, med: 0, low: 0 }}, posture: [{{ rating: 'good', not_checked: [] }}] }});
R.gap = h.headline({{ scanned: true, findings_by_severity: {{ high: 0, med: 0, low: 0 }}, posture: [{{ rating: 'unknown', not_checked: ['dns'] }}] }});
R.low = h.headline({{ scanned: true, findings_by_severity: {{ high: 0, med: 0, low: 2 }}, posture: [] }}).text;
R.views = [h.cleanView('detailed'), h.cleanView('simple'), h.cleanView(null), h.cleanView('x')];
console.log(JSON.stringify(R));""")
    assert out["rows"][0][:3] == ["A", "At risk", "bad"]
    assert out["rows"][1][1:3] == ["Not checked", ""] and out["rows"][1][3] == "Not checked: name lookups, downloads"
    assert out["rows"][2][1:3] == ["Protected", "ok"]
    assert out["top"]["text"] == "2 things need attention" and out["top"]["tone"] == "bad" and "1 area was not checked" in out["top"]["sub"]
    assert out["none"] == "No scan has finished yet" and out["nul"] == "No scan has finished yet"
    assert out["clean"]["text"] == "Nothing found on the last scan" and out["clean"]["tone"] == "ok"
    assert out["gap"]["tone"] == "" and "not checked" in out["gap"]["sub"], "a collector that did not answer is never a green all-clear"
    assert out["low"] == "Only minor notes on the last scan"
    assert out["views"] == ["detailed", "simple", "simple", "simple"]


def test_security_has_a_simple_and_a_detailed_view_and_a_wifi_summary():
    src = (_SCREENS / "security" / "index.js").read_text(encoding="utf-8")
    css = (_SCREENS / "security" / "security.css").read_text(encoding="utf-8")
    assert 'data-view="simple"' in src and 'data-view="detailed"' in src and "ctx.prefs.set('view'" in src
    assert "/api/os/security/wifi" in src and "What this network can see" in src and "What it cannot see" in src
    assert '.sec[data-view="simple"] .sec-detail { display: none; }' in css
    for key in ("secMac", "secActivity", "secAnalyst", "secEgress", "secHow"):
        assert key in src


def test_the_wifi_card_states_only_what_the_server_sent():
    src = (_SCREENS / "security" / "index.js").read_text(encoding="utf-8")
    body = src[src.index("function paintWifi()"):src.index("function paintFlow()")]
    assert "w.sees" in body and "w.hidden" in body and "w.unknown" in body
    assert "VPN" in body and "Not checked" in body


def test_every_restyled_screen_mounts_in_a_real_browser_without_errors():
    """The first cut of the Security screen lost a constant and threw while mounting; only a real mount shows that."""
    pytest.importorskip("playwright")
    proc = subprocess.run(
        [str(_ROOT / ".venv" / "bin" / "python"), str(_ROOT / "scripts" / "os_shell_check.py"), "--port", "18799", "--json",
         "--screens", ",".join(FOLDERS),
         "--settle", "0.8"],
        capture_output=True, text=True, timeout=240, check=False,
    )
    if "Executable doesn't exist" in proc.stdout + proc.stderr or "channel" in proc.stderr and "chrome" in proc.stderr and proc.returncode:
        pytest.skip("Chrome is not installed here")
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["problems"] == [], report["problems"]
    for visit in report["visits"]:
        assert visit["page_errors"] == [] and visit["console_errors"] == [], visit["screen"]
