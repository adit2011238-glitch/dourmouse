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
