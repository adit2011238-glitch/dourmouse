"""FIX-P R5-18: the egress ledger is outside the workspace, in a protected folder, and tamper-evident."""

from __future__ import annotations

import json
from pathlib import Path

from dourmouse import privacy
from dourmouse.config import user_config_dir, workspace_dir


def _rows(n: int) -> None:
    for i in range(n):
        privacy.record_egress(backend="gemini", model="g", text=f"request {i}")


def test_the_ledger_is_in_the_settings_folder_not_the_workspace():
    _rows(1)
    path = privacy.egress_path()
    assert path.is_relative_to(user_config_dir()) and not path.is_relative_to(workspace_dir())


def test_privacy_is_a_protected_workspace_folder_for_every_file_tool():
    from dourmouse import general_roster, sandbox

    assert "privacy" in sandbox._PROTECTED_WORKSPACE_DIRS
    assert "privacy" in general_roster._PROTECTED_WORKSPACE_TOP


def test_chain_is_ok_and_detects_a_deleted_edited_or_inserted_row():
    _rows(4)
    assert privacy.egress_chain_status()["ok"] is True
    path = privacy.egress_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["seq"] for x in lines] == [1, 2, 3, 4]
    # a deleted row
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    status = privacy.egress_chain_status()
    assert status["ok"] is False and status["broken_at"] == 3
    # an edited row
    edited = json.loads(lines[1])
    edited["bytes"] = 0
    path.write_text("\n".join([lines[0], json.dumps(edited, sort_keys=True), *lines[2:]]) + "\n", encoding="utf-8")
    assert privacy.egress_chain_status()["ok"] is False
    # a truncated file that no longer starts the chain is not flagged (the start is unknown); an appended forged
    # row with a wrong prev is
    path.write_text("\n".join(lines) + "\n" + json.dumps({"seq": 5, "prev": "0" * 32}) + "\n", encoding="utf-8")
    assert privacy.egress_chain_status()["ok"] is False


def test_chain_continues_across_rotation(monkeypatch):
    monkeypatch.setattr(privacy, "EGRESS_MAX_BYTES", 900)
    _rows(12)
    assert Path(f"{privacy.egress_path()}.1").exists()
    assert privacy.egress_chain_status()["ok"] is True
