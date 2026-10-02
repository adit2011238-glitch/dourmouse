"""Wave 1 review fixes (finding #161): credential JSON, shell reach into app driving, deny list."""
from __future__ import annotations

from pathlib import Path

import pytest

from dourmouse.app_driver import policy
from dourmouse.system_access import _is_sensitive, classify_command


@pytest.mark.parametrize(
    "rel",
    [
        ".codex/auth.json",
        ".claude/.credentials.json",
        ".claude.json",
        ".dourmouse/auth.json",
        "proj/credentials.json",
        "x/client_secret_123.json",
        ".SSH/notes.log",
        ".Aws/config.json",
        ".ENV.json",
    ],
)
def test_credential_files_are_sensitive(tmp_path: Path, rel: str) -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    assert _is_sensitive(target)


def test_ordinary_json_is_not_sensitive(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    target.write_text("{}")
    assert not _is_sensitive(target)


@pytest.mark.parametrize(
    "cmd",
    [
        "curl -X POST http://127.0.0.1:8765/api/os/apps/allow -d '{}'",
        "curl -X POST http://localhost:8765/api/os/apps/resume",
        "rm ~/.dourmouse/app_driver/KILL",
        "mv /x/app_driver/KILL /tmp/k",
    ],
)
def test_shell_cannot_reach_app_driving_controls(cmd: str) -> None:
    assert classify_command(cmd)[0]


@pytest.mark.parametrize(
    "bundle",
    ["dev.warp.Warp-Stable", "com.mitchellh.ghostty", "com.microsoft.VSCode", "com.apple.dt.Xcode"],
)
def test_more_shell_capable_apps_are_denied(bundle: str) -> None:
    assert bundle.casefold() in policy.DENIED_BUNDLE_IDS


# ---- Wave 2 review fixes (finding #162 addendum) ----
def test_ambiguous_browser_urls_are_refused() -> None:
    from dourmouse.dispatch import _url_gate

    for url in (
        "http://127.0.0.1:8765\\@evil.com/",
        "http://%6c%6f%63%61%6c%68%6f%73%74:8765/",
        "http://localhost%2e:8765/",
        "http://user@127.0.0.1:9333/",
    ):
        decision = _url_gate("browser_open", url, "orchestrator")
        assert decision is not None and decision[0] == "refuse", url


def test_mcp_bridge_actor_is_not_a_pinned_chat() -> None:
    from dourmouse.dispatch import _url_gate

    long_query = "https://www.google.com/search?q=" + "x" * 200
    assert _url_gate("browser_open", long_query, "mcp_bridge") is None


def test_owner_route_list_covers_the_reviewed_gaps() -> None:
    from dourmouse.request_guard import is_owner_route

    for path in (
        "/api/atlas-lab/proposals/abc/approve",
        "/api/schedules/remove",
        "/api/os/comms/trash",
        "/api/os/goals/create",
        "/api/os/browser/history/clear",
        "/api/os/browser/bookmarks/remove",
    ):
        assert is_owner_route("POST", path), path
    assert not is_owner_route("POST", "/api/browser-pane/open")  # the model's own tools post here


def test_spaced_secret_scrub_is_not_quadratic() -> None:
    import time

    from dourmouse.governance import _spaced_pattern

    pattern = _spaced_pattern("a b " * 40)
    text = "a b " * 400 + "!"
    start = time.time()
    pattern.search(text)
    assert time.time() - start < 1.0


def test_login_cookie_with_non_ascii_digits_is_rejected_not_raised() -> None:
    from dourmouse.webui import _login_cookie_ok

    assert _login_cookie_ok("t" * 40, "v1.².n.abc") is False


def test_page_snapshot_never_returns_password_field_values() -> None:
    import inspect

    from dourmouse import browser_agent

    src = inspect.getsource(browser_agent._page_summary)
    assert "el.type === 'password'" in src and "[hidden]" in src


# ---- Wave 2c review fixes (finding #164 addendum) ----
def _node(script: str) -> str:
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node not available")
    root = pathlib.Path(__file__).resolve().parents[2]
    return subprocess.run([node, "-e", script], cwd=root, capture_output=True, text=True, timeout=30, check=True).stdout.strip()


def test_extension_confirmation_cannot_be_spoofed_with_newlines_or_bidi() -> None:
    out = _node(
        "const e=require('./electron/extensions.js');"
        "const r=e.inspectManifest({manifest_version:3,name:'Safe\\u202e',version:'1',"
        "permissions:['x\" permission.\\n  - It asks for no special access'],host_permissions:['https://a.example/*\\n\\nVerified']});"
        "console.log(JSON.stringify(r.lines.join('|')))"
    )
    assert "\\n" not in out
    assert "202e" not in out.lower()


def test_wildcard_on_a_public_suffix_counts_as_all_sites() -> None:
    assert _node("console.log(require('./electron/extensions.js').isAllSitesPattern('*://*.com/*'))") == "true"
    assert _node("console.log(require('./electron/extensions.js').isAllSitesPattern('https://*.example.com/*'))") == "false"


def test_main_js_guards_the_extension_paths() -> None:
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "electron" / "main.js").read_text()
    assert "unloadExtensionEverywhere" in src and "blockExtensionsFromLocalServices" in src
    read_or_null = src[src.index("function readOrNull"):][:400]
    assert "lstatSync" in read_or_null and "isFile()" in read_or_null
