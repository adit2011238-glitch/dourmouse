"""The lockdown extension: manifest validity, least privilege, and the node
harness that runs background.js against stubbed chrome APIs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[2] / "extension" / "lockdown"
NODE = shutil.which("node")


def _manifest():
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_is_mv3_with_least_privilege():
    m = _manifest()
    assert m["manifest_version"] == 3
    assert m["permissions"] == ["declarativeNetRequest", "alarms", "storage"]
    assert m["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert m["background"] == {"service_worker": "background.js"}
    assert "content_scripts" not in m and "web_accessible_resources" not in m
    assert "optional_permissions" not in m and "optional_host_permissions" not in m
    assert (EXT / m["background"]["service_worker"]).is_file()


def test_manifest_has_the_fields_chrome_requires():
    m = _manifest()
    assert m["name"] and m["description"] and len(m["description"]) <= 132
    parts = m["version"].split(".")
    assert 1 <= len(parts) <= 4 and all(p.isdigit() for p in parts)


def test_the_worker_loads_no_remote_code():
    src = (EXT / "background.js").read_text(encoding="utf-8")
    assert "importScripts" not in src and "eval(" not in src and "new Function" not in src
    assert "https://" not in src and src.count("http://127.0.0.1") == 1


def test_the_readme_states_the_honest_limits():
    text = (EXT / "README.md").read_text(encoding="utf-8").lower()
    for needle in ("load unpacked", "developer mode", "allow in incognito", "not protected", "hosts file"):
        assert needle in text


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_background_js_parses_and_the_harness_passes():
    check = subprocess.run([NODE, "--check", str(EXT / "background.js")], capture_output=True, text=True, timeout=60, check=False)
    assert check.returncode == 0, check.stderr
    run = subprocess.run([NODE, str(EXT / "test_background.mjs")], capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "tests passed" in run.stdout
