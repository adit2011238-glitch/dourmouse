"""Finding #144: every folder under ui/assets/os/screens/ obeys the screen contract.

Architecture section 2.3: no direct fetch, EventSource, setInterval or global
key/window listeners (they go through ctx so unmount can clean them up); no
innerHTML except through the kit; every file passes node --check; a default
export with mount(); CSS scoped to the screen. This test is generic: a screen
builder adds a folder and it is checked with no edit here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_SCREENS = _OS / "screens"
_NODE = shutil.which("node")

FOLDERS = sorted(p for p in _SCREENS.iterdir() if p.is_dir())
IDS = [p.name for p in FOLDERS]

FORBIDDEN = [
    (r"\bfetch\s*\(", "fetch( (use ctx.api)"),
    (r"new\s+EventSource\b", "new EventSource (use ctx.events)"),
    (r"\bsetInterval\s*\(", "setInterval( (use ctx.every)"),
    (r"document\s*\.\s*addEventListener\s*\(\s*['\"]key", "a global keydown listener (use ctx.keys.bind)"),
    (r"window\s*\.\s*addEventListener\s*\(", "window.addEventListener (use ctx)"),
    (r"\.innerHTML\s*[+]?=(?!=)", "an innerHTML assignment (use the kit's html and setHtml)"),
    (r"\.outerHTML\s*=(?!=)", "an outerHTML assignment"),
    (r"insertAdjacentHTML\s*\(", "insertAdjacentHTML"),
    (r"document\s*\.\s*write\s*\(", "document.write"),
    (r"\beval\s*\(", "eval("),
    (r"new\s+Function\s*\(", "new Function("),
    (r"\bsetTimeout\s*\(\s*['\"]", "a string passed to setTimeout"),
    (r"\son[a-z]+\s*=\s*['\"]", "an inline event handler attribute"),
    (r"javascript:", "a javascript: URL"),
    (r"target\s*=\s*['\"]?_blank", "target=_blank (use ctx.host.openExternal)"),
    (r"localStorage|sessionStorage", "direct storage (use ctx.prefs)"),
    (r"new\s+WebSocket\b|\bXMLHttpRequest\b|navigator\.sendBeacon", "a network primitive other than ctx.api"),
]


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"`])//[^\n]*", r"\1", js)


def scan(folder: Path):
    for path in sorted(folder.rglob("*.js")):
        yield path, strip_comments(path.read_text(encoding="utf-8"))


def top_level_selectors(css: str):
    """Selectors of every rule, including those nested in @media/@supports."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[str] = []

    def walk(text: str, in_keyframes: bool = False) -> None:
        i = 0
        while i < len(text):
            j = text.find("{", i)
            if j == -1:
                return
            head = text[i:j].strip()
            depth, k = 1, j + 1
            while k < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[j + 1 : k - 1]
            if head.startswith("@"):
                if head.startswith(("@keyframes", "@-webkit-keyframes")):
                    out.append("@keyframes " + head.split()[-1])
                else:
                    walk(body)
            elif not in_keyframes:
                out.extend(s.strip() for s in head.split(",") if s.strip())
            i = k

    walk(css)
    return out


def test_the_folder_list_is_not_empty():
    assert {"home", "security"} <= set(IDS)


@pytest.mark.parametrize("folder", FOLDERS, ids=IDS)
class TestEachScreen:
    def test_has_an_index_and_only_known_file_kinds(self, folder):
        assert (folder / "index.js").is_file()
        for p in folder.rglob("*"):
            if p.is_file():
                assert p.suffix in {".js", ".css"}, f"{p} is not a js or css file"

    def test_no_forbidden_calls(self, folder):
        for path, code in scan(folder):
            for pattern, what in FORBIDDEN:
                m = re.search(pattern, code)
                assert not m, f"{path.relative_to(_ROOT)} uses {what}: ...{code[max(0, m.start() - 30):m.end() + 30]!r}"

    def test_no_em_dash_and_no_untrusted_markup_shortcuts(self, folder):
        for path in folder.rglob("*"):
            if path.is_file():
                assert "—" not in path.read_text(encoding="utf-8"), f"em dash in {path.name}"

    def test_every_file_passes_node_check(self, folder):
        if _NODE is None:
            pytest.skip("node not on PATH in this environment")
        for path in sorted(folder.rglob("*.js")):
            proc = subprocess.run([_NODE, "--check", str(path)], capture_output=True, text=True, timeout=30, check=False)
            assert proc.returncode == 0, f"{path}: {proc.stderr}"

    def test_default_export_has_mount_and_matches_its_folder(self, folder, tmp_path):
        if _NODE is None:
            pytest.skip("node not on PATH in this environment")
        script = tmp_path / "probe.mjs"
        script.write_text(
            f"import m from {(folder / 'index.js').as_uri()!r};\n"
            "console.log(JSON.stringify({ id: m.id, mount: typeof m.mount, css: Boolean(m.css), thread: Boolean(m.thread), sub: typeof m.sub, unmount: typeof m.unmount, refresh: typeof m.refresh }));\n",
            encoding="utf-8",
        )
        proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        import json

        info = json.loads(proc.stdout.strip().splitlines()[-1])
        assert info["mount"] == "function", "the default export needs an async mount(root, ctx)"
        assert info["id"] == folder.name.upper(), "the screen id must be its folder name in capitals"
        assert info["sub"] == "string"
        assert info["unmount"] in {"undefined", "function"} and info["refresh"] in {"undefined", "function"}
        assert info["css"] == (folder / f"{folder.name}.css").is_file(), "css: true if and only if <id>.css exists"
        registry = (_OS / "core" / "registry.js").read_text(encoding="utf-8")
        m = re.search(rf"S\('{folder.name.upper()}', (true|false)", registry)
        assert m, "the screen is not in the registry"
        assert (m.group(1) == "true") == info["thread"], "thread flag must match the registry"

    def test_css_is_scoped_to_the_screen(self, folder):
        css = folder / f"{folder.name}.css"
        if not css.is_file():
            pytest.skip("this screen has no stylesheet")
        prefix = f'[data-screen="{folder.name.upper()}"]'
        bad = [s for s in top_level_selectors(css.read_text(encoding="utf-8")) if not s.startswith(prefix) and not s.startswith("@keyframes")]
        assert not bad, f"selectors not scoped to {prefix}: {bad[:5]}"

    def test_screen_css_does_not_hardcode_the_amber_accent(self, folder):
        css = folder / f"{folder.name}.css"
        if not css.is_file():
            pytest.skip("this screen has no stylesheet")
        text = css.read_text(encoding="utf-8").lower()
        assert not re.search(r"245\s*,\s*158\s*,\s*11|#f59e0b|#ffb224", text), "use color-mix() on var(--dm-active), or accent changes leave amber behind"


class TestTheContractCheckerItself:
    """A checker that cannot fail proves nothing: feed it code that must be caught."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "const r = await fetch('/api/x');",
            "el.innerHTML = value;",
            "el.innerHTML += value;",
            "setInterval(tick, 1000);",
            "document.addEventListener('keydown', f);",
            "window.addEventListener('resize', f);",
            "new EventSource('/api/events');",
            "localStorage.setItem('a', 'b');",
            "a.setAttribute('x', 1); b.onclick = 'x'; c = '<a onclick=\"go()\">';",
        ],
    )
    def test_a_violation_is_detected(self, snippet):
        code = strip_comments(snippet)
        assert any(re.search(p, code) for p, _ in FORBIDDEN), snippet

    def test_comments_and_urls_are_not_flagged(self):
        code = strip_comments("/* fetch( is banned */\n// setInterval( too\nconst u = 'https://example.com/a'; // note fetch(\nel.textContent = 'x';")
        assert not any(re.search(p, code) for p, _ in FORBIDDEN)

    def test_the_css_walker_finds_nested_and_grouped_selectors(self):
        sels = top_level_selectors("[data-screen=\"A\"] a, .b { x: y } @media (max-width: 1px) { .c { x: y } } @keyframes k { from { a: b } to { a: b } }")
        assert sels == ['[data-screen="A"] a', ".b", ".c", "@keyframes k"]
