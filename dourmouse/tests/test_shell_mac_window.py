"""Finding #194 (T6 milestone 1): the shell as a real Mac window.

Covers the macOS appearance bridge (electron/appearance.js) and the page side that applies it
with a contrast guard (core/appearance.js), the real menu bar (electron/appmenu.js) and its
agreement with the page's command table and shortcuts, the eight places, pinned projects, the
retired fake chrome, and the style check (scripts/ui_style_lint.mjs). The modules run in node,
as in test_os_core.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_EL = _ROOT / "electron"
_NODE = shutil.which("node") or ""

pytestmark = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def run(tmp_path: Path, body: str, imports: str = "") -> dict:
    header = re.sub(
        r"from '((?:core|kit|chrome)/[^']+)'",
        lambda m: f"from '{(_OS / m.group(1)).as_uri()}'",
        imports,
    )
    script = tmp_path / "harness.mjs"
    script.write_text(header + "\nconst R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_cjs(tmp_path: Path, body: str) -> dict:
    script = tmp_path / "harness.cjs"
    script.write_text("const R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestAppearanceOnThePage:
    IMPORTS = "import { tokensFor, ensureContrast, accentFill, composite } from 'core/appearance.js';\nimport { contrast } from 'core/prefs.js';"

    def test_every_text_colour_reads_at_4_5_to_1_whatever_macos_hands_over(self, tmp_path):
        out = run(tmp_path, """
const accents = ['#007aff', '#ffcc00', '#a2845e', '#8e8e93', '#ff2d55', '#34c759', '#ffffff', '#000000'];
R.rows = [];
for (const dark of [false, true]) for (const accent of accents) {
  const content = dark ? '#1e1e1e' : '#ffffff';
  const t = tokensFor({ dark, accent, colors: {
    controlBackground: { hex: content, alpha: 1 },
    secondaryLabel: { hex: dark ? '#ffffff' : '#000000', alpha: 0.5 },
    placeholder: { hex: dark ? '#ffffff' : '#000000', alpha: 0.25 },
    link: { hex: dark ? '#419cff' : '#0068da', alpha: 1 },
    good: { hex: dark ? '#32d74b' : '#28cd41', alpha: 1 },
    warning: { hex: dark ? '#ff9f0a' : '#ff9500', alpha: 1 },
    critical: { hex: dark ? '#ff453a' : '#ff3b30', alpha: 1 },
  } });
  for (const k of ['--tk-text-2', '--tk-text-3', '--tk-link', '--tk-accent-text', '--tk-good-text', '--tk-warning-text', '--tk-critical-text']) {
    R.rows.push([dark, accent, k, contrast(t.vars[k], content)]);
  }
  R.rows.push([dark, accent, 'white on accent-fill', contrast('#ffffff', t.vars['--tk-accent-fill'])]);
}
""", self.IMPORTS)
        bad = [r for r in out["rows"] if r[3] < 4.5]
        assert not bad, bad

    def test_theme_and_flags_follow_macos_and_the_browser_falls_back(self, tmp_path):
        out = run(tmp_path, """
R.sys = tokensFor({ dark: true, highContrast: true, reduceMotion: true, reduceTransparency: true, accent: '#ff9500', colors: {} });
R.browserDark = tokensFor(null, true).theme; R.browserLight = tokensFor(null, false).theme;
R.fallbackAccent = tokensFor(null, false).vars['--tk-accent'];
R.composite = composite('#000000', 0.5, '#ffffff');
R.already = ensureContrast('#000000', '#ffffff');
R.fill = accentFill('#007aff');
""", self.IMPORTS)
        assert out["sys"]["theme"] == "dark" and out["sys"]["contrast"] == "more"
        assert out["sys"]["reduceMotion"] is True and out["sys"]["reduceTransparency"] is True
        assert out["browserDark"] == "dark" and out["browserLight"] == "light"
        assert out["fallbackAccent"] == "#007aff"
        assert out["composite"] == "#808080" and out["already"] == "#000000"
        assert out["fill"] != "#007aff"  # Apple's blue carries white text at about 4:1, so it is darkened

    def test_only_a_hex_colour_can_reach_a_custom_property(self, tmp_path):
        out = run(tmp_path, """
const set = {};
const root = { style: { setProperty: (k, v) => { set[k] = v; }, removeProperty: (k) => { delete set[k]; } }, dataset: {} };
const { createAppearance } = await import('""" + (_OS / "core" / "appearance.js").as_uri() + """');
const a = createAppearance({ win: { matchMedia: () => ({ matches: false, addEventListener() {} }) }, root });
a.apply({ dark: false, accent: 'red;}body{display:none', colors: { label: { hex: 'url(x)', alpha: 1 }, separator: { hex: '#123456', alpha: 1 } } });
R.set = set; R.theme = root.dataset.theme;
""", self.IMPORTS)
        assert out["theme"] == "light"
        assert all(re.fullmatch(r"#[0-9a-f]{6}", v) for v in out["set"].values()), out["set"]
        assert out["set"]["--tk-accent"] == "#007aff"  # the hostile accent fell back
        assert "--tk-text" not in out["set"]  # the hostile label was dropped


class TestAppearanceBridge:
    def test_reads_macos_and_validates_every_colour(self, tmp_path):
        out = run_cjs(tmp_path, f"""
const lib = require({str(_EL / 'appearance.js')!r});
const nativeTheme = {{ shouldUseDarkColors: true, shouldUseHighContrastColors: false, prefersReducedTransparency: true, on() {{}}, removeListener() {{}} }};
const sp = {{
  getAccentColor: () => 'ff9500ff',
  getColor: (n) => (n === 'label' ? '#ffffffd8' : n === 'separator' ? 'not a colour' : '#1e1e1eff'),
  getSystemColor: (n) => ({{ green: '#32d74b', orange: '#ff9f0a', red: '#ff453a', blue: '#0a84ff' }})[n],
  getAnimationSettings: () => ({{ prefersReducedMotion: true }}),
}};
R.a = lib.readAppearance({{ nativeTheme, systemPreferences: sp, platform: 'darwin' }});
R.linux = lib.readAppearance({{ nativeTheme, systemPreferences: sp, platform: 'linux' }});
R.throws = lib.readAppearance({{ nativeTheme, systemPreferences: {{ getAccentColor() {{ throw new Error('x'); }} }}, platform: 'darwin' }});
""")
        a = out["a"]
        assert a["dark"] is True and a["reduceTransparency"] is True and a["reduceMotion"] is True
        assert a["accent"] == "#ff9500"
        assert a["colors"]["label"] == {"hex": "#ffffff", "alpha": 0.847}
        assert "separator" not in a["colors"]  # a bad value is dropped, never passed on
        assert a["colors"]["good"]["hex"] == "#32d74b"
        assert out["linux"]["accent"] is None and out["linux"]["colors"] == {}
        assert out["throws"]["accent"] is None  # an OS call that throws never stops the app

    def test_main_answers_only_the_console_and_watches_for_changes(self):
        main = (_EL / "main.js").read_text(encoding="utf-8")
        assert 'ipcMain.handle("appearance:get", (evt) => (consoleOnly(evt) ? currentAppearance() : null));' in main
        assert 'if (consoleOnly(evt) && appMenu) appMenu.update(state);' in main
        assert "appearanceLib.watchAppearance(" in main and "installShellMenuAndAppearance();" in main
        assert 'title: "Dourmouse"' in main and "DOURMOUSE // CENTRAL AGENT DISPATCH" not in main
        pre = (_EL / "preload.js").read_text(encoding="utf-8")
        assert 'ipcRenderer.invoke("appearance:get")' in pre and '"menu:command"' in pre


class TestMenuBar:
    def test_menu_commands_page_commands_and_shortcuts_agree(self, tmp_path):
        menu = run_cjs(tmp_path, f"""
const m = require({str(_EL / 'appmenu.js')!r});
const t = m.buildTemplate({{ send: () => {{}}, state: {{ sidebar: true, inspector: false }} }});
R.ids = m.COMMAND_IDS;
R.tops = t.map((x) => x.label || x.role);
const flat = [];
const walk = (items) => items.forEach((i) => {{ if (i.submenu) walk(i.submenu); else if (i.accelerator) flat.push([i.label, i.accelerator]); }});
walk(t);
R.acc = flat;
R.view = t[3].submenu.map((i) => i.label).filter(Boolean);
const hidden = m.buildTemplate({{ send: () => {{}}, state: {{ sidebar: false, inspector: true }} }});
R.viewHidden = hidden[3].submenu.map((i) => i.label).filter(Boolean);
""")
        page = run(tmp_path, """
R.ids = COMMAND_IDS;
const l = shortcutList(PLACES);
R.keys = l.filter((s) => s.combo && s.bound !== false && s.id !== 'undo').map((s) => [s.id, s.combo]);
""", "import { COMMAND_IDS } from 'core/commands.js';\nimport { shortcutList } from 'core/shortcuts.js';\nimport { PLACES } from 'core/registry.js';")
        assert sorted(menu["ids"]) == sorted(page["ids"])
        assert menu["tops"] == ["Dourmouse", "File", "editMenu", "View", "Go", "windowMenu", "help"]
        assert "Hide Sidebar" in menu["view"] and "Show Inspector" in menu["view"]
        assert "Show Sidebar" in menu["viewHidden"] and "Hide Inspector" in menu["viewHidden"]
        # the same keys on the menu and in the page keymap
        to_electron = lambda c: c.replace("Meta", "Command").replace("Ctrl", "Control")
        menu_acc = {a.lower() for _, a in menu["acc"]}
        for cid, combo in page["keys"]:
            if cid in ("search",):
                assert to_electron(combo).lower() in menu_acc, cid
                continue
            assert to_electron(combo).lower() in menu_acc, (cid, combo)

    def test_a_command_id_the_menu_does_not_know_never_reaches_the_page(self):
        main = (_EL / "main.js").read_text(encoding="utf-8")
        assert "if (!appMenuLib.COMMAND_IDS.includes(id)) return;" in main

    def test_the_command_table_refuses_unknown_ids_and_requires_every_action(self, tmp_path):
        out = run(tmp_path, """
const ran = [];
const acts = {}; COMMAND_IDS.filter((i) => !i.startsWith('place:')).forEach((i) => { acts[i] = () => ran.push(i); });
acts.place = (s) => ran.push('go:' + s);
const c = createCommands(acts);
R.unknown = c.run('rm -rf'); R.ok = c.run('sidebar'); R.place = c.run('place:library');
R.ran = ran;
try { createCommands({ place() {} }); R.missing = false; } catch (e) { R.missing = e.message; }
""", "import { createCommands, COMMAND_IDS } from 'core/commands.js';")
        assert out["unknown"] is False and out["ok"] is True and out["place"] is True
        assert out["ran"] == ["sidebar", "go:library"]
        assert out["missing"] and "settings" in out["missing"]


class TestPlaces:
    IMPORTS = "import { PLACES, SCREENS, placeOf, placeBySlug, VIEW_LABELS } from 'core/registry.js';\nimport { resolveRoute } from 'core/router.js';"

    def test_eight_places_hold_every_screen_exactly_once(self, tmp_path):
        out = run(tmp_path, """
R.places = PLACES.map((p) => p.label);
R.views = PLACES.flatMap((p) => p.views);
R.screens = SCREENS.map((s) => s.id);
R.labels = R.screens.filter((id) => !VIEW_LABELS[id]);
R.office = placeOf('COMMAND_CENTER').label; R.lib = placeBySlug('library').views[0];
R.route = resolveRoute('#/library').id; R.routeOld = resolveRoute('#/comms').id;
""", self.IMPORTS)
        assert out["places"] == ["Home", "Projects", "Office", "Research", "Library", "Browser", "Security", "Settings"]
        assert sorted(out["views"]) == sorted(out["screens"]) and len(out["views"]) == len(set(out["views"]))
        assert out["labels"] == []
        assert out["office"] == "Office" and out["lib"] == "WIKI"
        assert out["route"] == "WIKI" and out["routeOld"] == "COMMS"  # old routes keep working

    def test_pins_are_cleaned_before_they_are_kept(self, tmp_path):
        out = run(tmp_path, """
R.c = cleanPins(['/a', '/a', '', 3, '/b\\u0007', '/c', ...Array.from({ length: 20 }, (_, i) => '/p' + i)]);
R.bad = cleanPins('nope');
R.cover = [coverIndex('Biology IA'), coverIndex('Biology IA'), coverIndex('')];
""", "import { cleanPins } from 'core/prefs.js';\nimport { coverIndex } from 'chrome/sidebar.js';")
        assert out["c"][:2] == ["/a", "/c"] and len(out["c"]) == 12
        assert out["bad"] == []
        assert out["cover"][0] == out["cover"][1] and all(1 <= c <= 6 for c in out["cover"])


class TestWindowSkeleton:
    def test_the_fake_operating_system_chrome_is_gone(self):
        html = (_ROOT / "ui" / "shell.html").read_text(encoding="utf-8")
        for gone in ('id="menubar"', 'id="dock"', 'id="controlcenter"', 'id="notifcenter"', 'id="wallpicker"', 'id="specbar"', 'id="lights"', "os-wallpaper"):
            assert gone not in html, gone
        for kept in ('id="sidebar"', 'id="stagebar"', 'id="viewswitch"', 'id="inspector"', 'id="sheetlayer"', 'id="wall"', 'data-shell="os"'):
            assert kept in html, kept
        for mod in ("menubar", "dock", "control-centre", "wallpaper", "shortcuts-panel"):
            assert not (_OS / "chrome" / f"{mod}.js").exists(), mod
        css = html.index("/assets/os/tokens.css") < html.index("/assets/os/shell.css")
        assert css, "tokens load before the shell stylesheet"

    def test_badges_count_runs_not_feed_polls(self):
        status = (_OS / "chrome" / "status.js").read_text(encoding="utf-8")
        assert "if (s === 'computing' || s === 'auth') working += 1;" in status
        boot = (_OS / "boot.js").read_text(encoding="utf-8")
        assert "s.agents.working" in boot


class TestStyleLint:
    def test_the_shell_passes(self):
        proc = subprocess.run([_NODE, str(_ROOT / "scripts" / "ui_style_lint.mjs"), "--json"], capture_output=True, text=True, timeout=60, check=False)
        data = json.loads(proc.stdout)
        assert proc.returncode == 0 and data["found"] == [], data["found"]

    def test_it_catches_what_it_bans(self, tmp_path):
        out = run(tmp_path, """
const { lintCss, lintText } = await import('""" + (_ROOT / "scripts" / "ui_style_lint.mjs").as_uri() + """');
const css = ['a { background: linear-gradient(red, blue); }', 'b { text-shadow: 0 0 2px var(--x); }', 'c { text-transform: uppercase; letter-spacing: .1em; }',
  'd { color: #ff0000; }', 'e { box-shadow: 0 0 20px var(--tk-accent); }', 'f { font-family: Papyrus; }', 'g:focus { outline: none; }',
  'h { backdrop-filter: blur(20px); }', 'ok { box-shadow: var(--tk-shadow-float); letter-spacing: 0; }'].join('\\n');
R.css = lintCss(css, '/x/shell.css').map((f) => f.rule);
R.text = lintText('a \\u2014 b\\nfine\\nsmile \\u{1F600}', '/x/boot.js').map((f) => f.rule);
""")
        assert out["css"] == ["gradient", "text-shadow", "uppercase", "letter-spacing", "colour literal outside tokens.css",
                              "decorative shadow", "font-family outside the tokens", "focus outline removed", "backdrop blur"]
        assert out["text"] == ["em dash", "emoji"]


class TestAppearanceMismatch:
    def test_neutrals_from_the_other_appearance_are_not_used(self, tmp_path):
        """Seen live in the Electron run of finding #194: light pinned, dark system colours handed over."""
        out = run(tmp_path, """
const t = tokensFor({ dark: false, accent: '#007aff', colors: {
  controlBackground: { hex: '#1e1e1e', alpha: 1 }, secondaryLabel: { hex: '#ffffff', alpha: 0.55 },
  good: { hex: '#28cd41', alpha: 1 } } });
R.vars = t.vars;
""", "import { tokensFor } from 'core/appearance.js';")
        assert "--tk-bg-content" not in out["vars"] and "--tk-text-2" not in out["vars"]
        assert out["vars"]["--tk-good"] == "#28cd41" and out["vars"]["--tk-accent"] == "#007aff"
