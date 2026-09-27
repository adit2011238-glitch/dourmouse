"""UX fix round (2026-09-27): stage actions, dock, shortcuts, thread view, contrast tokens.

Harnesses import the shipped modules from ui/assets/os in node, as test_os_core.py does.
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
_NODE = shutil.which("node") or ""

pytestmark = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


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


class TestStageActionIds:
    IMPORTS = "import { assertUniqueActions, actionKey, dedupeActions } from 'kit/actions.js';"

    def test_a_duplicate_id_in_one_call_is_refused(self, tmp_path):
        out = run(tmp_path, """
try { assertUniqueActions([{ id: 'new', label: 'NEW QUESTION' }, { id: 'new', label: 'NEW THREAD' }]); R.threw = false; }
catch (e) { R.threw = true; R.msg = e.message; }
""", self.IMPORTS)
        assert out["threw"] is True and "new" in out["msg"]

    def test_a_label_counts_as_the_id_when_there_is_none(self, tmp_path):
        out = run(tmp_path, """
try { assertUniqueActions([{ label: 'SCAN' }, { id: 'SCAN', label: 'x' }]); R.threw = false; } catch (e) { R.threw = true; }
R.key = actionKey({ label: 'SCAN' });
""", self.IMPORTS)
        assert out["threw"] is True and out["key"] == "SCAN"

    def test_distinct_ids_pass_and_an_action_without_a_name_is_refused(self, tmp_path):
        out = run(tmp_path, """
assertUniqueActions([{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }]); R.ok = true;
try { assertUniqueActions([{}]); R.empty = false; } catch (e) { R.empty = true; }
""", self.IMPORTS)
        assert out["ok"] is True and out["empty"] is True

    def test_a_repeated_key_is_made_unique_so_no_button_is_dropped(self, tmp_path):
        out = run(tmp_path, """
const fixed = dedupeActions([{ id: 'new', label: 'A' }, { id: 'new', label: 'B' }, { label: 'C' }, { label: 'C' }, {}]);
R.ids = fixed.map((a) => a.id || a.label);
R.labels = fixed.map((a) => a.label);
assertUniqueActions(fixed); R.clean = true;
""", self.IMPORTS)
        assert out["ids"] == ["new", "new~2", "C", "C~2", "action"]
        assert out["labels"] == ["A", "B", "C", "C", "action"] and out["clean"] is True

    def test_the_stage_calls_the_check_and_the_thread_ids_no_longer_collide(self):
        stage = (_OS / "chrome" / "stage.js").read_text(encoding="utf-8")
        assert "assertUniqueActions(actions)" in stage
        assert "dedupeActions(actions)" in stage, "a duplicate id must be reported and survived, not thrown into the screen"
        tv = (_OS / "kit" / "thread-view.js").read_text(encoding="utf-8")
        assert "id: 'thread-new'" in tv and "id: 'thread-auto'" in tv
        research = (_OS / "screens" / "research" / "index.js").read_text(encoding="utf-8")
        assert "id: 'new-question'" in research


class TestShortcuts:
    IMPORTS = "import { shortcutList, keysFor, bindable } from 'core/shortcuts.js';\nimport { SCREENS } from 'core/registry.js';\nimport { createKeymap } from 'core/keymap.js';"

    def test_the_first_nine_screens_in_sidebar_order_get_command_1_to_9(self, tmp_path):
        out = run(tmp_path, """
const l = shortcutList(SCREENS);
R.screens = l.filter((s) => s.group === 'Screens').map((s) => [s.combo, s.screen]);
R.settings = keysFor(l, 'SETTINGS'); R.home = keysFor(l, 'HOME'); R.help = keysFor(l, 'help'); R.newc = keysFor(l, 'new');
R.combos = bindable(l).map((s) => s.combo);
""", self.IMPORTS)
        ids = ["HOME", "COMMS", "RESEARCH", "BROWSER", "MEDIA", "CODE", "PROJECTS", "WIKI", "GOALS"]
        assert out["screens"] == [[f"Meta+{i + 1}", ids[i]] for i in range(9)]
        assert out["settings"] == "⌘," and out["home"] == "⌘1"
        assert out["help"] == "⌘/" and out["newc"] == "⌘N"
        assert "Meta+," in out["combos"] and "Meta+/" in out["combos"] and "Meta+n" in out["combos"]
        assert "Alt+a" not in out["combos"] and "Escape" not in out["combos"]

    def test_every_bound_combo_works_while_typing_and_escape_is_untouched(self, tmp_path):
        out = run(tmp_path, """
const l = shortcutList(SCREENS); const km = createKeymap(); const fired = [];
bindable(l).forEach((s) => km.bind(s.combo, () => fired.push(s.id), { editable: true }));
const field = { tagName: 'TEXTAREA' };
const key = (k) => km.handle({ key: k, metaKey: true, target: field });
key(','); key('1'); key('9'); key('n'); key('/'); key('0');
R.fired = fired;
R.esc = km.handle({ key: 'Escape', target: field });
""", self.IMPORTS)
        assert out["fired"] == ["settings", "screen1", "screen9", "new", "help"]
        assert out["esc"] is False  # nothing pushed on the Esc stack: unchanged behaviour

    def test_boot_binds_them_and_the_launcher_and_help_panel_use_the_same_list(self):
        boot = (_OS / "boot.js").read_text(encoding="utf-8")
        assert "bindable(shortcuts)" in boot and "editable: true" in boot
        pal = (_OS / "chrome" / "palette.js").read_text(encoding="utf-8")
        assert "keysFor(shortcuts" in pal and "act:shortcuts" in pal
        assert "tab_id: scope.tabId()" in pal  # the launcher's new conversation used to send no tab id (HTTP 400)
        assert 'id="shortcuts"' in (_ROOT / "ui" / "shell.html").read_text(encoding="utf-8")


class TestMarkdownBlocks:
    IMPORTS = "import { md } from 'kit/md.js';"

    def test_tables_quotes_headings_rules_and_code_blocks_render_safely(self, tmp_path):
        out = run(tmp_path, r"""
const src = '# Big\n> quoted <b>x</b>\n\n| A | B |\n|:--|--:|\n| 1 | <i>2</i> |\n\n---\n*em* and **b**\n\n```js\nlet a = "<x>";\n```\n';
R.html = md(src).text;
""", self.IMPORTS)
        h = out["html"]
        assert '<div class="md-h md-h1">Big</div>' in h
        assert "<blockquote>quoted &lt;b&gt;x&lt;/b&gt;</blockquote>" in h
        assert '<table class="md-table"><thead><tr><th>A</th><th class="md-al-right">B</th></tr></thead>' in h
        assert "<td class=\"md-al-right\">&lt;i&gt;2&lt;/i&gt;</td>" in h
        assert "<hr>" in h and "<i>em</i>" in h and "<b>b</b>" in h
        assert 'class="md-copy"' in h and 'data-md-copy="1"' in h and '<span class="md-lang">js</span>' in h
        assert "let a = &quot;&lt;x&gt;&quot;;" in h
        assert "<script" not in h and "<b>x</b>" not in h and "<i>2</i>" not in h
        assert " style=" not in h  # the page CSP has no inline styles to lean on

    def test_new_block_regexes_are_bounded_on_200kb_of_the_worst_shapes(self, tmp_path):
        out = run(tmp_path, r"""
const N = 200000; const times = {};
const cases = { pipes: '|'.repeat(N), pipeLines: '|a|b\n|-|-\n'.repeat(N / 10), dividers: '|-'.repeat(N / 2) + '\n' + '|-'.repeat(N / 2),
  quotes: '>'.repeat(N), quoteLines: '> a\n'.repeat(N / 4), stars: '*a '.repeat(N / 3), starsTight: '*a*'.repeat(N / 3), rules: '---\n'.repeat(N / 4),
  hashes: '#'.repeat(N), headLine: '# ' + 'a'.repeat(N), rows: ('| a | b |\n|---|---|\n' + '| x | y |\n'.repeat(3000)).repeat(4) };
for (const [k, v] of Object.entries(cases)) { const t = Date.now(); md(v); times[k] = Date.now() - t; }
R.max = Math.max(...Object.values(times)); R.times = times;
""", self.IMPORTS)
        assert out["max"] < 3000, out["times"]

    def test_a_table_is_capped_in_rows_and_columns(self, tmp_path):
        out = run(tmp_path, r"""
const head = Array.from({ length: 60 }, (_, i) => 'c' + i).join('|');
const div = Array.from({ length: 60 }, () => '-').join('|');
const rows = Array.from({ length: 900 }, () => 'x|y').join('\n');
const html = md(head + '\n' + div + '\n' + rows).text;
R.th = (html.match(/<th[ >]/g) || []).length; R.tr = (html.match(/<tr>/g) || []).length;
""", self.IMPORTS)
        assert out["th"] == 20 and out["tr"] == 301


class TestThreadView:
    def test_reply_and_user_text_share_one_token_and_the_button_says_retry_after_an_error(self):
        tv = (_OS / "kit" / "thread-view.js").read_text(encoding="utf-8")
        assert "'RETRY'" in tv and "wireCodeCopy(reply" in tv
        for slug in ("home", "comms", "news", "media"):
            css = (_OS / "screens" / slug / f"{slug}.css").read_text(encoding="utf-8")
            you = re.search(r"\.turn \.you \{[^}]*font-size: ([^;]+);", css).group(1)
            reply = re.search(r"\.reply \{[^}]*font-size: ([^;]+);", css).group(1)
            assert you == reply == "var(--dm-text-base)", (slug, you, reply)


class TestStageLights:
    def test_the_lookalike_traffic_lights_are_hidden_inside_electron_only(self):
        stage = (_OS / "chrome" / "stage.js").read_text(encoding="utf-8")
        assert "host.kind === 'electron'" in stage and "lights.hidden = true" in stage
        boot = (_OS / "boot.js").read_text(encoding="utf-8")
        assert "go: (slug) => router.go(slug), host," in boot
