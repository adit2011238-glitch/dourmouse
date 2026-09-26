"""Finding #149: the AGENTSMITH screen's helpers (node) and the rules its source must keep.

The gate this screen draws is the human review of code a model wrote. The
helpers decide what a status may claim ("live" only when the server said so)
and what an approval says it approves; the source rules pin that APPROVE is
only ever built beside the code, that the approval carries the hash of what was
read, and that nothing here is a sample from the mockup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "agentsmith"
_NODE = shutil.which("node")
SHA = "ab" * 32


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "s.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestSignature:
    def test_derived_from_the_schema_and_always_returns_str(self, tmp_path):
        out = node(tmp_path, """
R.a = h.signature('flag_message', { type: 'object', properties: { message_id: { type: 'string' }, on: { type: 'boolean' }, n: { type: 'integer' }, xs: { type: 'array', items: { type: 'string' } }, o: { type: 'object' } }, required: ['message_id'] });
R.none = h.signature('ping', { type: 'object', properties: {} });
R.bad = h.signature('x', null); R.weird = h.signature('x', { properties: { a: 5, b: { type: 'zzz' } } });""")
        assert out["a"] == "flag_message(message_id: str, on: bool = ..., n: int = ..., xs: list[str] = ..., o: dict = ...) -> str"
        assert out["none"] == "ping() -> str" and out["bad"] == "x() -> str"
        assert out["weird"] == "x(a: Any = ..., b: Any = ...) -> str"

    def test_a_huge_schema_is_capped(self, tmp_path):
        out = node(tmp_path, "const p = {}; for (let i = 0; i < 500; i++) p['a' + i] = { type: 'string' }; R.s = h.signature('t', { properties: p });")
        assert out["s"].count(":") == 12


class TestStatus:
    def test_approved_is_not_live(self, tmp_path):
        out = node(tmp_path, """
R.d = h.statusInfo({ status: 'DRAFTED' });
R.live = h.statusInfo({ status: 'APPROVED', live: true });
R.restart = h.statusInfo({ status: 'APPROVED', live: false });
R.unknown = h.statusInfo({ status: 'APPROVED', live: null });
R.unset = h.statusInfo({ status: 'APPROVED' });
R.failed = h.statusInfo({ status: 'APPROVAL_FAILED' }); R.rej = h.statusInfo({ status: 'REJECTED' }); R.odd = h.statusInfo({ status: 'WEIRD' });""")
        assert out["d"]["word"] == "awaiting review" and out["d"]["group"] == "pending"
        assert out["live"]["word"] == "approved and live" and out["live"]["tone"] == "ok"
        assert out["restart"]["word"] == "approved, restart required" and out["restart"]["tone"] == "warn"
        assert out["unknown"]["word"] == "approved, not confirmed live" and out["unset"]["word"] == "approved, not confirmed live"
        assert out["failed"]["tone"] == "bad" and out["rej"]["group"] == "rejected" and out["odd"]["group"] == "other"

    def test_only_a_confirmed_live_tool_ever_gets_the_ok_tone(self, tmp_path):
        out = node(tmp_path, "R.t = [true, false, null, undefined].map((live) => h.statusInfo({ status: 'APPROVED', live }).tone);")
        assert out["t"] == ["ok", "warn", "warn", "warn"]


class TestLede:
    def test_counts_come_from_the_board_and_a_restart_is_stated(self, tmp_path):
        out = node(tmp_path, """
R.a = h.ledeLine({ total: 3, shown: 3, counts: { DRAFTED: 1, APPROVED: 2 }, restart_needed: 1 });
R.b = h.ledeLine({ total: 1, shown: 1, counts: {}, restart_needed: 0 });
R.c = h.ledeLine({ total: 300, shown: 200, counts: { DRAFTED: 0 }, restart_needed: 0 });
R.none = h.ledeLine(null);
R.r1 = h.restartLine({ restart_needed: 1 }); R.r2 = h.restartLine({ restart_needed: 2 }); R.r0 = h.restartLine({ restart_needed: 0 });""")
        assert out["a"] == "3 drafts. 1 awaiting your review. 1 approved, not live until a restart."
        assert out["b"] == "1 draft. 0 awaiting your review."
        assert "showing the newest 200" in out["c"] and out["none"] == ""
        assert "Every call will still ask for your confirmation" in out["r1"] and out["r2"].startswith("2 approved tools are")
        assert out["r0"] == ""

    def test_the_board_signature_ignores_what_a_reader_is_reading(self, tmp_path):
        out = node(tmp_path, """
const d = { id: 'a', status: 'DRAFTED', live: undefined, decided_at: null, decision_reason: null, description: 'x' };
R.same = h.boardSignature({ drafts: [d] }) === h.boardSignature({ drafts: [{ ...d, description: 'changed text' }] });
R.moved = h.boardSignature({ drafts: [d] }) !== h.boardSignature({ drafts: [{ ...d, status: 'APPROVED' }] });
R.live = h.boardSignature({ drafts: [{ ...d, status: 'APPROVED', live: false }] }) !== h.boardSignature({ drafts: [{ ...d, status: 'APPROVED', live: true }] });""")
        assert out == {"same": True, "moved": True, "live": True}


class TestApproval:
    def test_the_approval_says_what_is_approved_and_that_a_restart_follows(self, tmp_path):
        out = node(tmp_path, f"R.p = h.approvePrompt({{ tool_name: 'greet', module_lines: 24, preview_sha256: '{SHA}' }}); R.r = h.rejectPrompt({{ tool_name: 'greet' }});")
        assert "greet" in out["p"] and "24-line" in out["p"] and SHA[:12] in out["p"]
        assert "not callable until the server restarts" in out["p"] and "every call still asks for your confirmation" in out["p"]
        assert "can never be approved" in out["r"]

    def test_a_draft_that_cannot_be_read_cannot_be_approved(self, tmp_path):
        out = node(tmp_path, """
R.ok = h.whyNotApprovable({ status: 'DRAFTED', module_preview: 'x', too_large_to_review: false });
R.big = h.whyNotApprovable({ status: 'DRAFTED', module_preview: '', too_large_to_review: true });
R.empty = h.whyNotApprovable({ status: 'DRAFTED', module_preview: '', too_large_to_review: false });
R.done = h.whyNotApprovable({ status: 'APPROVED', module_preview: 'x' }); R.nothing = h.whyNotApprovable(null);""")
        assert out["ok"] == "" and out["done"] == ""
        assert "too large" in out["big"] and "no module text" in out["empty"] and out["nothing"]

    def test_disk_line_is_a_warning_on_mismatch_and_silent_when_unknown(self, tmp_path):
        out = node(tmp_path, """
R.ok = h.diskLine({ status: 'APPROVED', on_disk_matches: true }); R.bad = h.diskLine({ status: 'APPROVED', on_disk_matches: false });
R.unk = h.diskLine({ status: 'APPROVED', on_disk_matches: null }); R.dr = h.diskLine({ status: 'DRAFTED', on_disk_matches: true });""")
        assert "matches" in out["ok"] and out["bad"].startswith("WARNING") and out["unk"] == "" and out["dr"] == ""


class TestDraftDirective:
    def test_the_directive_limits_the_model_to_drafting(self, tmp_path):
        out = node(tmp_path, "R.d = h.draftDirective('  flag a mail message  ');")
        assert out["d"].startswith("Use your draft_tool to draft")
        assert "you cannot approve" in out["d"] and out["d"].endswith("Capability gap: flag a mail message")

    def test_the_check(self, tmp_path):
        out = node(tmp_path, "R.a = h.draftCheck('short'); R.b = h.draftCheck('x'.repeat(1501)); R.c = h.draftCheck('a tool that flags a mail message');")
        assert out["a"]["ok"] is False and out["b"]["ok"] is False and out["c"] == {"ok": True, "error": ""}


class TestSource:
    def read(self, name):
        return (_DIR / name).read_text(encoding="utf-8")

    def test_no_mockup_sample_data_is_left(self):
        text = self.read("index.js") + self.read("helpers.js") + self.read("agentsmith.css")
        for sample in ("gmail_flag", "dev_coding", "IMAP STORE", "most restrictive permission tier", "Live-verified end to end", "Registers the tool"):
            assert sample not in text, sample

    def test_approve_and_reject_are_built_only_inside_the_opened_detail(self):
        js = self.read("index.js")
        detail = js[js.index("function detailNode"):js.index("function codeBlock")]
        assert "dataset.approve" in detail and "dataset.reject" in detail
        outside = js.replace(detail, "")
        assert not re.search(r"dataset\.(approve|reject)\s*=", outside), "a decision button must be created only beside the code"
        assert not re.search(r"'(APPROVE|REJECT)'", outside), "the labels are only ever built in the detail"
        assert detail.index("module_preview") < detail.index("dataset.approve"), "the module text is added before APPROVE is"
        assert "whyNotApprovable" in detail, "an unreadable draft gets no APPROVE"

    def test_the_approval_carries_the_hash_of_what_was_read_and_asks_first(self):
        js = self.read("index.js")
        assert "sha256: d.preview_sha256" in js
        approve = js[js.index("function approve("):js.index("function reject(")]
        assert "ask(approvePrompt(d)" in approve and "'/api/os/agentsmith/approve'" in approve
        assert "'/api/self_extensions/approve'" not in js, "the hash-checked route is the only approval the screen calls"

    def test_every_write_is_inside_a_confirmation(self):
        js = self.read("index.js")
        posts = [m.start() for m in re.finditer(r"ctx\.api\.post\(", js)]
        assert len(posts) == 2, "approve and reject only"
        for i in posts:
            assert "ask(" in js[max(0, i - 700):i], "each write is the body of ask(...), which is confirmHere"

    def test_drafting_hands_a_directive_to_the_agent_and_registers_nothing(self):
        js = self.read("index.js")
        assert "focusAgent: 'agent_smith'" in js and "draftDirective(" in js
        assert "ctx.chat.send(" in js and js.count("ctx.chat.send(") == 1

    def test_untrusted_text_only_goes_in_as_text(self):
        js = self.read("index.js")
        assert "innerHTML" not in js and "insertAdjacentHTML" not in js

    def test_the_copy_states_the_restart(self):
        text = self.read("index.js") + self.read("helpers.js")
        assert "restart" in text.lower() and "always asks for your confirmation" in text
