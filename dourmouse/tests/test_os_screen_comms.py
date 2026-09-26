"""Finding #149: the COMMS screen's helpers (node) and the rules its source must keep.

The helpers decide what a mail row claims and what the compose form may send.
A helper that guessed a time, an unread flag or a recipient would be a fake
figure, so the empty paths are tested as hard as the good ones.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "comms"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "s.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestSender:
    def test_name_and_address(self, tmp_path):
        out = node(tmp_path, """
R.a = h.senderName('GitHub <noreply@github.com>'); R.b = h.senderName('"Doe, Jane" <j@x.co>');
R.c = h.senderName('plain@x.co'); R.d = h.senderName(''); R.e = h.senderName('<only@x.co>');
R.f = h.senderAddress('GitHub <noreply@github.com>'); R.g = h.senderAddress('plain@x.co'); R.h = h.senderAddress('no address here');""")
        assert (out["a"], out["b"], out["c"], out["d"], out["e"]) == ("GitHub", "Doe, Jane", "plain@x.co", "", "only@x.co")
        assert (out["f"], out["g"], out["h"]) == ("noreply@github.com", "plain@x.co", "")

    def test_a_hostile_sender_is_plain_text_and_fast(self, tmp_path):
        t = time.perf_counter()
        node(tmp_path, "h.senderName('\"' + '<'.repeat(200000)); h.senderName('a'.repeat(200000) + ' <'); h.senderAddress('<'.repeat(200000)); h.senderName('\"'.repeat(200000));")
        assert time.perf_counter() - t < 5


class TestWhen:
    NOW = "new Date(2026, 8, 26, 18, 0).getTime()"

    def test_today_this_year_and_older(self, tmp_path):
        out = node(tmp_path, """
const now = NOW_EXPR;
R.today = h.whenLabel({ ts: new Date(2026, 8, 26, 17, 2).getTime() }, now);
R.year = h.whenLabel({ ts: new Date(2026, 8, 24, 9, 0).getTime() }, now);
R.old = h.whenLabel({ ts: new Date(2025, 2, 2, 9, 0).getTime() }, now);""".replace("NOW_EXPR", self.NOW))
        assert out == {"today": "17:02", "year": "Sep 24", "old": "2025-03-02"}

    def test_a_bad_or_missing_time_is_never_a_wrong_time(self, tmp_path):
        out = node(tmp_path, """
R.none = h.whenLabel(null); R.empty = h.whenLabel({ ts: 0, date: '' }); R.junk = h.whenLabel({ ts: 0, date: 'not a date at all really' });
R.imap = h.whenLabel({ ts: 0, date: '2026-09-26 17:02' }, new Date(2026, 8, 26, 18, 0).getTime());""")
        assert out["none"] == "" and out["empty"] == ""
        assert out["junk"] == "not a date at al", "an unparseable date is shown as the server sent it, cut to 16 characters"
        assert out["imap"] == "17:02"


class TestAge:
    def test_says_cached_only_when_the_server_did(self, tmp_path):
        out = node(tmp_path, """
const now = 1790000000000;
R.live = h.listAge({ cached_at: 1790000000 - 3, cached: false }, now);
R.cached = h.listAge({ cached_at: 1790000000 - 125, cached: true, ttl: 180 }, now);
R.none = h.listAge({ cached_at: null }, now); R.nothing = h.listAge(null, now);""")
        assert out["live"] == "Read just now" and "cached" not in out["live"]
        assert out["cached"] == "Read 2m ago (cached, refreshes after 3 min)"
        assert out["none"] == "" and out["nothing"] == ""


class TestCompose:
    def test_valid_and_each_refusal(self, tmp_path):
        out = node(tmp_path, """
const ok = { to: 'a@b.co', subject: 'Hi', body: 'x' };
R.ok = h.composeCheck(ok);
R.noto = h.composeCheck({ ...ok, to: '' });
R.badto = h.composeCheck({ ...ok, to: 'not an address' });
R.two = h.composeCheck({ ...ok, to: 'a@b.co, c@d.co' });
R.angle = h.composeCheck({ ...ok, to: 'A <a@b.co>' });
R.nosub = h.composeCheck({ ...ok, subject: '  ' });
R.multi = h.composeCheck({ ...ok, subject: 'a\\nb' });
R.quote = h.composeCheck({ ...ok, subject: 'say "hi"' });
R.nobody = h.composeCheck({ ...ok, body: ' \\n ' });""")
        assert out["ok"]["ok"] is True
        for k in ("noto", "badto", "two", "angle", "nosub", "multi", "quote", "nobody"):
            assert out[k]["ok"] is False and out[k]["error"], k

    def test_the_directive_is_the_consoles_fixed_sentence_with_the_exact_body(self, tmp_path):
        out = node(tmp_path, "R.d = h.composeDirective({ to: ' a@b.co ', subject: ' Hi ', body: 'line one\\n\\nline two  ' });")
        assert out["d"] == 'Send an email to a@b.co with subject "Hi" and this exact body:\n\nline one\n\nline two  '

    def test_the_address_pattern_is_fast_on_hostile_input(self, tmp_path):
        t = time.perf_counter()
        node(tmp_path, "h.composeCheck({ to: '@'.repeat(200000), subject: 's', body: 'b' }); h.composeCheck({ to: 'a'.repeat(200000) + '@' + 'b.'.repeat(100000), subject: 's', body: 'b' });")
        assert time.perf_counter() - t < 5

    def test_reply_fills_the_sender_and_a_single_re(self, tmp_path):
        out = node(tmp_path, """
R.a = h.replyDraft({ from: 'GitHub <n@g.com>', subject: 'Build failed' }, null);
R.b = h.replyDraft({ from: 'x@y.co', subject: 'Re: Build failed' }, null);
R.c = h.replyDraft({ from: 'row@y.co', subject: 'row subj' }, { from: 'Full <full@y.co>', subject: 'Full subj' });""")
        assert out["a"] == {"to": "n@g.com", "subject": "Re: Build failed", "body": ""}
        assert out["b"]["subject"] == "Re: Build failed"
        assert out["c"]["to"] == "full@y.co" and out["c"]["subject"] == "Re: Full subj"


class TestPrompts:
    def test_each_prompt_names_the_message_and_what_will_happen(self, tmp_path):
        out = node(tmp_path, """
const row = { subject: 'Quarterly numbers', from: 'Finance <f@x.co>' };
for (const k of ['archive', 'trash', 'flag', 'unflag']) R[k] = h.actionPrompt(k, row);
R.none = h.actionPrompt('delete', row);""")
        for k in ("archive", "trash", "flag", "unflag"):
            assert "Quarterly numbers" in out[k] and "Finance" in out[k], k
        assert "stays in All Mail" in out["archive"] and "Nothing is deleted" in out["archive"]
        assert "30 days" in out["trash"] and "not deleted permanently" in out["trash"]
        assert out["none"] == "", "there is no prompt, so no action, for anything that is not one of the four"

    def test_the_done_line_prefers_the_servers_sentence(self, tmp_path):
        out = node(tmp_path, "R.a = h.doneLine('trash', { message: 'GMAIL TRASHED: x' }); R.b = h.doneLine('trash', {}); R.c = h.doneLine('zzz', null);")
        assert out == {"a": "GMAIL TRASHED: x", "b": "Moved to Trash. Gmail keeps it for 30 days.", "c": "Done."}

    def test_row_updates(self, tmp_path):
        out = node(tmp_path, """
const rows = [{ id: 'a', flagged: false }, { id: 'b', flagged: true }];
R.f = h.withFlag(rows, 'a', true); R.g = h.withoutRow(rows, 'b'); R.n = h.withoutRow(null, 'b');""")
        assert out["f"][0]["flagged"] is True and out["f"][1]["flagged"] is True
        assert [r["id"] for r in out["g"]] == ["a"] and out["n"] == []

    def test_the_unavailable_hint_says_what_to_do_in_each_mode(self, tmp_path):
        out = node(tmp_path, "R.o = h.unavailableHint('oauth'); R.i = h.unavailableHint('imap'); R.n = h.unavailableHint('none');")
        assert "did not answer" in out["o"] and "App Password" in out["i"] and "/login" in out["n"]


class TestSource:
    def read(self, name):
        return (_DIR / name).read_text(encoding="utf-8")

    def test_no_mockup_sample_data_is_left(self):
        text = self.read("index.js") + self.read("helpers.js") + self.read("comms.css")
        for sample in ("Tailscale", "Figma", "MYP personal project", "recon-2026-09-11", "17:02", "IMAP STORE", "mail agent caches for 300s"):
            assert sample not in text, sample

    def test_a_row_change_always_goes_through_a_confirm_card(self):
        js = self.read("index.js")
        posts = re.findall(r"ctx\.api\.post\(", js)
        assert len(posts) == 1, "exactly one write call, inside the confirmation"
        i = js.index("ctx.api.post(")
        before = js[max(0, i - 400):i]
        assert "confirmHere(" in before, "the write is the body of a confirmHere run"

    def test_sending_is_never_a_direct_call(self):
        js = self.read("index.js")
        assert "/api/os/comms/send" not in js and "gmail_send" not in js.replace("whose gmail_send tool", "")
        assert "focusAgent: 'mail'" in js, "compose hands a directive to the mail agent, whose gate asks"

    def test_the_only_untrusted_text_paths_are_text_content(self):
        js = self.read("index.js")
        assert "innerHTML" not in js and "insertAdjacentHTML" not in js

    def test_every_button_has_a_spec_sentence(self):
        js = self.read("index.js")
        n = len(re.findall(r"\bbutton\(\s*[^,]+,\s*", js)) - 1  # the helper's own definition line
        assert n >= 10
        assert "el('button'" not in js.replace("const b = el('button', 'os-btn'", "").replace("el('button', 'cm-open')", ""), "a bare button would have no data-spec"
