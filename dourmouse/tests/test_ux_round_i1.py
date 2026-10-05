"""UX round I1 (#170): setup and login on the shell tokens, Text size, toast actions and undo,
the sign-in banner, plain unavailable states, and the small pure helpers behind them.

Node harnesses import the shipped modules from ui/assets/os, as test_os_ux_fixes.py does."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_NODE = shutil.which("node") or ""
needs_node = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def run(tmp_path: Path, body: str, imports: str = "") -> dict:
    header = re.sub(
        r"from '((?:core|kit|chrome|screens)/[^']+)'",
        lambda m: f"from '{(_OS / m.group(1)).as_uri()}'",
        imports,
    )
    script = tmp_path / "harness.mjs"
    script.write_text(header + "\nconst R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class TestGatePages:
    def test_both_pages_use_the_shell_green_and_no_cream(self):
        for rel in ("ui/login.html", "ui/setup.html"):
            src = _read(rel)
            assert "#071D18" in src and "#F59E0B" in src, rel
            assert "#F7F5F1" not in src and "#FFFFFF;" not in src.split("</style>")[0].split(":root")[1].split("}")[0], rel

    def test_pages_are_self_contained_for_a_device_that_cannot_fetch_assets(self):
        for rel in ("ui/login.html", "ui/setup.html"):
            head = _read(rel).split("</style>")[0]
            assert "radial-gradient" in head, "the wallpaper is inline, not an asset"

    def test_login_keeps_every_id_and_route_the_flows_depend_on(self):
        src = _read("ui/login.html")
        for needle in ("ggoogleBtn", "ggoogleMsg", "showManualLink", "/api/auth/google/start", "/api/auth/claim", "/api/login",
                       "reason === 'not_allowed'", "claimed === '1'", "startClaimPoll", "glogout", "Sign in with Google"):
            assert needle in src, needle

    def test_login_user_copy_has_no_environment_variable_outside_the_developer_note(self):
        src = _read("ui/login.html")
        for var in ("GOOGLE_OAUTH_FULL_SCOPES", "DOURMOUSE_ALLOWED_EMAILS", "GOOGLE_CLIENT_ID"):
            for m in re.finditer(var, src):
                before = src[max(0, m.start() - 400):m.start()]
                assert "setDevNote(" in before, f"{var} is shown outside the developer note"

    def test_setup_keeps_its_handlers_endpoints_and_the_ungated_skip(self):
        src = _read("ui/setup.html")
        for needle in ("/api/setup/save", "/api/setup/validate-key", "/api/setup/probe-node", "/api/setup/restart",
                       "/api/auth/google/start?claim=", "/api/auth/claim?code=", 'onclick="show(2)"', "pywebview",
                       'id="gSkipBtn" onclick="gAdvance()">Skip<'):
            assert needle in src, needle
        assert ".hide{display:none!important}" in src, "a hidden card must stay hidden whatever else sets display"

    def test_setup_welcome_lists_the_three_things_it_can_reach(self):
        src = _read("ui/setup.html")
        for word in ("Read and write files", "Run commands", "Send mail as you"):
            assert word in src


@needs_node
class TestTextSize:
    IMPORTS = "import { createPrefs, TEXT_SIZES } from 'core/prefs.js';"

    def test_a_valid_size_is_stored_applied_and_defaults_otherwise(self, tmp_path):
        out = run(tmp_path, """
const mem = {}; const ls = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); }, removeItem: (k) => { delete mem[k]; } };
const el = { dataset: {}, style: { setProperty() {} } };
const calls = [];
const api = { post: async (path, body) => { calls.push([path, body]); return { ok: true }; } };
const prefs = createPrefs({ storage: ls, api, root: el });
R.def = prefs.textSize();
prefs.applyTextSize('large'); const r = await prefs.save('textSize', 'large');
R.attr = el.dataset.textSize; R.stored = prefs.textSize(); R.ok = r.ok; R.call = calls[0];
prefs.applyTextSize('default'); R.cleared = el.dataset.textSize === undefined;
mem['dm.textSize'] = 'huge'; R.bad = prefs.textSize();
R.ids = TEXT_SIZES.map((t) => t.id);
""", self.IMPORTS)
        assert out["def"] == "default" and out["attr"] == "large" and out["stored"] == "large" and out["ok"] is True
        assert out["call"] == ["/api/state/prefs", {"key": "os.textSize", "value": "large"}]
        assert out["cleared"] is True and out["bad"] == "default" and out["ids"] == ["small", "default", "large"]

    def test_css_never_goes_below_11px_and_large_raises_every_token(self):
        css = _read("ui/assets/os/shell.css")
        small = re.search(r':root\[data-text-size="small"\] \{([^}]*)\}', css).group(1)
        large = re.search(r':root\[data-text-size="large"\] \{([^}]*)\}', css).group(1)
        sizes = [float(x) for x in re.findall(r"--dm-text-[a-z0-9]+: ([0-9.]+)px", small)]
        assert sizes and min(sizes) >= 11
        assert "--dm-text-2xs: 12.5px" in large and "--dm-text-2xl" in large

    def test_the_flowcharts_are_at_least_11_units_and_never_shrink(self):
        for rel in ("ui/assets/os/kit/flow-svg.js", "ui/assets/os/screens/research/graph-svg.js"):
            src = _read(rel)
            sizes = [float(x) for x in re.findall(r'font-size="([0-9.]+)"', src)]
            assert sizes and min(sizes) >= 11, rel
            assert "min-width:calc(" in src and "flowscroll" in src, rel


@needs_node
class TestToastsAndUndo:
    def test_an_action_toast_runs_its_action_and_command_z_finds_only_undo_actions(self, tmp_path):
        out = run(tmp_path, """
const els = [];
globalThis.document = { createElement: () => ({ className: '', textContent: '', append() {}, replaceChildren() {} }) };
globalThis.document.createElement = () => ({ className: '', textContent: '' });
const mount = { replaceChildren() {} };
const tpl = { content: { firstElementChild: { querySelector: () => ({ addEventListener() {} }), addEventListener() {} } }, set innerHTML(v) {} };
globalThis.document.createElement = (t) => (t === 'template' ? tpl : { className: '', textContent: '' });
const { createToasts } = await import('""" + (_OS / "chrome" / "toasts.js").as_uri() + """');
const timers = { every() {} };
const t = createToasts({ mount, prefs: { dnd: () => false }, timers });
let undone = 0, opened = 0;
t.show({ title: 'plain' });
R.none = t.undoLast();
t.show({ title: 'link', action: { label: 'Open', onClick: () => { opened += 1; } } });
R.linkIsNotUndo = t.undoLast();
t.show({ title: 'archived', action: { label: 'Undo', undo: true, onClick: () => { undone += 1; } } });
R.has = t.hasUndo(); R.ran = t.undoLast(); R.undone = undone; R.hasAfter = t.hasUndo();
for (let i = 0; i < 6; i += 1) t.show({ title: 'n' + i });
R.visible = t.visibleCount();
const before = t.local().slice().reverse(); t.clearLocal(); R.cleared = t.local().length; t.restoreLocal(before); R.restored = t.local().length === before.length;
""")
        assert out["none"] is False and out["linkIsNotUndo"] is False
        assert out["has"] is True and out["ran"] is True and out["undone"] == 1 and out["hasAfter"] is False
        assert out["visible"] == 3, "the stack is capped at three"
        assert out["cleared"] == 0 and out["restored"] is True

    def test_boot_binds_command_z_and_the_shortcut_is_listed(self):
        boot = _read("ui/assets/os/boot.js")
        assert "undo: () => toasts.undoLast()" in boot
        assert "id: 'undo'" in _read("ui/assets/os/core/shortcuts.js")


@needs_node
class TestStartupBanner:
    def test_dismissed_items_stay_quiet_until_a_new_one_goes_missing(self, tmp_path):
        out = run(tmp_path, """
const items = missingSignins({ claude: { ok: false }, codex: { ok: true } }, { configured: true, me: null });
R.ids = items.map((i) => i.id);
R.after = unsilenced(items, ['google']).map((i) => i.id);
R.none = unsilenced(items, ['claude', 'google']).length;
R.bad = unsilenced(items, null).length;
""", "import { missingSignins, unsilenced } from 'chrome/startup-check.js';")
        assert out["ids"] == ["claude", "google"] and out["after"] == ["claude"] and out["none"] == 0 and out["bad"] == 2

    def test_the_banner_is_in_the_stage_and_the_floating_dialog_is_gone(self):
        html = _read("ui/shell.html")
        assert 'id="stagebanner"' in html and 'id="startup"' not in html
        assert '<main id="stage"' in html and 'id="skipLink"' in html


@needs_node
class TestPlainStates:
    def test_configuration_wording_moves_out_of_the_main_line(self, tmp_path):
        out = run(tmp_path, """
R.a = splitDeveloper('NOT CONFIGURED: set GOOGLE_GMAIL_USER and GOOGLE_GMAIL_APP_PASSWORD in .env. Nothing was fetched.');
R.b = splitDeveloper('The feed did not answer.');
R.c = splitDeveloper('Sign in with Google at /login, or set GOOGLE_GMAIL_USER, then refresh.');
""", "import { splitDeveloper } from 'kit/states.js';")
        assert out["a"] == {"plain": "Nothing was fetched.", "dev": "NOT CONFIGURED: set GOOGLE_GMAIL_USER and GOOGLE_GMAIL_APP_PASSWORD in .env."}
        assert out["b"] == {"plain": "The feed did not answer.", "dev": ""}
        assert out["c"]["plain"] == "" and "GOOGLE_GMAIL_USER" in out["c"]["dev"]


@needs_node
class TestSmallHelpers:
    def test_one_date_style_for_a_moment(self, tmp_path):
        out = run(tmp_path, """
const now = new Date(2026, 9, 5, 12, 0).getTime();
R.v = [new Date(2026, 9, 5, 7, 30), new Date(2026, 9, 6, 9, 0), new Date(2026, 9, 9, 9, 0), new Date(2026, 9, 20, 9, 0), new Date(2027, 0, 2, 9, 0), new Date(2026, 9, 4, 23, 0)].map((d) => whenShort(d.getTime(), now));
R.junk = whenShort('junk');
""", "import { whenShort } from 'kit/format.js';")
        assert out["v"] == ["07:30", "Tomorrow 09:00", "Fri 09:00", "20 Oct", "2 Jan 2027", "Yesterday 23:00"] and out["junk"] == ""

    def test_routines_read_as_sentences(self, tmp_path):
        out = run(tmp_path, """
R.s = ['every 30 minute(s)', 'daily at 07:30', 'every Monday at 09:00', 'every 1 hour(s)', 'every 2 day(s)'].map(scheduleSentence);
R.t = [toolLabel('check_mail'), toolLabel('some_odd_tool')]; R.a = argsPhrase('web_search', { query: 'nvidia earnings' });
""", "import { scheduleSentence, toolLabel, argsPhrase } from 'screens/timetable/helpers.js';")
        assert out["s"] == ["Every 30 minutes", "Every day at 07:30", "Every Monday at 09:00", "Every hour", "Every 2 days"]
        assert out["t"] == ["Check mail", "Some odd tool"] and out["a"] == 'for "nvidia earnings"'

    def test_the_goals_header_counts_each_state_separately_and_blocked_tasks_are_on_hold(self, tmp_path):
        out = run(tmp_path, """
const g = (status) => ({ status });
R.s = boardSummary([g('EXECUTING'), g('BLOCKED'), g('PAUSED'), g('COMPLETED')]);
R.none = boardSummary([]);
R.hold = taskTag('READY', 'BLOCKED').word; R.ready = taskTag('READY', 'EXECUTING').word; R.compat = taskTag('COMPLETED').word;
""", "import { boardSummary, taskTag } from 'screens/goals/helpers.js';")
        assert out["s"] == "1 running, 1 blocked, 1 paused, 1 finished" and out["none"] == "no goals yet"
        assert out["hold"] == "on hold" and out["ready"] == "ready" and out["compat"] == "done"


class TestSourceRules:
    def test_no_em_dash_in_the_files_this_round_touched(self):
        for rel in ("ui/assets/os/chrome/toasts.js", "ui/assets/os/chrome/startup-check.js",
                    "ui/assets/os/kit/states.js", "ui/assets/os/kit/format.js", "ui/assets/os/core/prefs.js"):
            assert "—" not in _read(rel) and "–" not in _read(rel), rel
