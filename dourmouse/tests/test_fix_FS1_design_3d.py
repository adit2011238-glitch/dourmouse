"""FS1 P4-16: the ungated manifest readers must not read arbitrary JSON files."""

from __future__ import annotations

import json
import os

import pytest

from dourmouse.design_3d_ops import build_design_3d_tool_specs


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "design_3d").mkdir(parents=True)
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    monkeypatch.delenv("DOURMOUSE_UI_MANIFEST_PATH", raising=False)
    return ws


def _handler(name):
    return {t.name: t for t in build_design_3d_tool_specs()}[name].handler


def _creds(tmp_path):
    vault = tmp_path / "project" / "data" / "browser_creds.json"
    vault.parent.mkdir(parents=True)
    vault.write_text(json.dumps({"bank.example": {"username": "ann", "password": "hunter2"}}))
    return vault


def test_read_entry_refuses_a_non_manifest_json_file(workspace, tmp_path):
    vault = _creds(tmp_path)
    out = _handler("read_manifest_entry")({"name": "bank.example", "manifest_path": str(vault)})
    assert "hunter2" not in out
    assert out.startswith("REFUSED")
    out = _handler("read_manifest_entry")({"name": "nope", "manifest_path": str(vault)})
    assert "bank.example" not in out


def test_list_refuses_a_non_manifest_json_file(workspace, tmp_path):
    vault = _creds(tmp_path)
    out = _handler("list_manifest")({"manifest_path": str(vault)})
    assert "bank.example" not in out
    assert out.startswith("REFUSED")


def test_symlink_named_like_a_manifest_is_judged_by_its_target(workspace, tmp_path):
    vault = _creds(tmp_path)
    link = tmp_path / "ui_manifest.json"
    os.symlink(vault, link)
    out = _handler("read_manifest_entry")({"name": "bank.example", "manifest_path": str(link)})
    assert "hunter2" not in out


def test_manifests_still_readable(workspace, tmp_path):
    inside = workspace / "design_3d" / "other.json"
    inside.write_text(json.dumps({"btn": {"category": "button"}}))
    assert '"btn"' in _handler("read_manifest_entry")({"name": "btn", "manifest_path": str(inside)})
    elsewhere = tmp_path / "spatial_ai_library" / "ui_components" / "ui_manifest.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(json.dumps({"card": {"category": "panel"}}))
    assert '"card"' in _handler("read_manifest_entry")({"name": "card", "manifest_path": str(elsewhere)})
    assert "card" in _handler("list_manifest")({"manifest_path": str(elsewhere)})
