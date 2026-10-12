"""PRIV-5 and PRIV-4 screens: the SETTINGS privacy section, the SECURITY egress section and the
chat toolbar's Private switch. Pure helpers run under node; the wiring is checked in source (the
DOM is verified in the real app)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_NODE = shutil.which("node")


def node(tmp_path: Path, module: Path, body: str) -> dict:
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {module.as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
                      encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_settings_consent_cards_say_what_is_sent_and_what_is_not(tmp_path):
    out = node(tmp_path, _OS / "screens" / "settings" / "helpers.js", """
const src = { id: 'mail', label: 'Mail', what: 'message subjects, snippets and bodies' };
R.allow = h.consentPrompt(src, 'allow');
R.deny = h.consentPrompt(src, 'deny');
R.ask = h.consentPrompt(src, 'ask');
R.on = h.privateModePrompt(true);
R.prov = h.providerPrompt({ label: 'Gemini', company: 'Google (AI Studio)' }, true);
R.tags = [h.providerTag({ configured: false }), h.providerTag({ configured: true, verified: false }),
          h.providerTag({ configured: true, verified: true })].map((t) => t.word);
R.choices = h.CONSENT_CHOICES.map((c) => c.id);
""")
    assert "Mail" in out["allow"] and "placeholders" in out["allow"] and "names and other text are not" in out["allow"]
    assert "Never send" in out["deny"] and "asks you once" in out["ask"]
    assert "never sent to a cloud model" in out["on"] and "Web search" in out["on"]
    assert "cannot check them" in out["prov"]
    assert out["tags"] == ["not set up", "not checked", "checked"]
    assert out["choices"] == ["ask", "allow", "deny"]
    for text in (out["allow"], out["deny"], out["ask"], out["on"], out["prov"]):
        assert "—" not in text


def test_security_egress_rows_show_metadata_only(tmp_path):
    out = node(tmp_path, _OS / "screens" / "security" / "helpers.js", """
const row = { ts: '2026-10-12T10:00:00Z', provider: 'Ollama Cloud (Ollama)', model: 'gpt-oss:120b', bytes: 2048,
  tiers: { T0: 1, T1: 0, T2: 2, T3: 1 }, sources: ['mail'], redactions: { EMAIL: 3 }, private: false };
R.r = h.egressRow(row);
R.sum = h.egressSummary({ summary: { requests: 2, by_provider: { 'Gemini (Google)': 2 }, with_sensitive: 1, write_failures: 0 } });
R.none = h.egressSummary({ summary: { requests: 0 } });
R.bad = h.egressSummary(null);
""")
    r = out["r"]
    assert r["provider"].startswith("Ollama Cloud") and r["size"] == "2.0 KB"
    assert r["tiers"] == "1 public, 2 sensitive, 1 withheld" and r["sources"] == "mail"
    assert r["redactions"] == "3 email replaced" and r["sensitive"] is True
    assert "Gemini (Google) 2" in out["sum"] and "No request" in out["none"] and "could not be read" in out["bad"]


def test_settings_screen_wires_every_privacy_write_through_a_confirm_card():
    src = (_OS / "screens" / "settings" / "index.js").read_text(encoding="utf-8")
    assert "/api/os/settings/privacy'" in src
    for path in ("/privacy/consent", "/privacy/private_mode", "/privacy/provider"):
        line = next(ln for ln in src.splitlines() if path in ln)
        assert line.strip().startswith("ask(") or "() => ctx.api.post" in line, line


def test_security_screen_reads_the_egress_ledger():
    src = (_OS / "screens" / "security" / "index.js").read_text(encoding="utf-8")
    assert "/api/os/privacy/egress" in src and "Sent to cloud models" in src


def test_chat_toolbar_has_a_private_switch_per_tab():
    src = (_OS / "kit" / "thread-view.js").read_text(encoding="utf-8")
    assert "id: 'thread-private'" in src
    assert "/api/os/settings/privacy/chat" in src and "tab_id: ctx.scope.tabId()" in src
    assert "/api/os/settings/privacy?tab_id=" in src
