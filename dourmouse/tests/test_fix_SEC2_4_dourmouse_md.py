"""SEC2-4 (high): <workspace>/DOURMOUSE.md is spliced into the system prompt of
every run, so a model steered by what it read must not be able to write it.

test_fix_SEC2_2_agent_config_files.py covers the confined file tools
(write_file, edit_file, delete_file) and the run_command shell. This file
drives the other writers end to end: write_path, apply_patch and
apply_search_replace on the real absolute path, in every case spelling, and
checks that the owner's text is what reaches system_message(). The marker
text is harmless.
"""

from __future__ import annotations

import pytest

from dourmouse import dispatch
from dourmouse import system_access
from dourmouse.dispatch import DispatchRegistry

_MARKER = "MARKER-SEC2-4-harmless"
_OWNER_TEXT = "Owner's own rules."


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(root))
    return root


def _prompt() -> str:
    return dispatch.system_message(DispatchRegistry())


@pytest.mark.parametrize("name", ["DOURMOUSE.md", "dourmouse.md", "Dourmouse.MD"])
def test_write_path_cannot_create_it(ws, name):
    out = system_access._write_path_tool({"path": str(ws / name), "content": _MARKER})
    assert out.startswith("REFUSED"), out
    assert not any(p.is_file() for p in ws.iterdir())
    assert _MARKER not in _prompt()


def test_write_path_cannot_overwrite_the_owners_file(ws):
    (ws / "DOURMOUSE.md").write_text(_OWNER_TEXT)
    out = system_access._write_path_tool({"path": str(ws / "DOURMOUSE.md"), "content": _MARKER})
    assert out.startswith("REFUSED"), out
    prompt = _prompt()
    assert _OWNER_TEXT in prompt and _MARKER not in prompt


def test_apply_search_replace_cannot_edit_it(ws):
    (ws / "DOURMOUSE.md").write_text(_OWNER_TEXT + "\n")
    patch = f"<<<<<<< SEARCH\n{_OWNER_TEXT}\n=======\n{_MARKER}\n>>>>>>> REPLACE\n"
    out = system_access._apply_search_replace_tool({"path": str(ws / "DOURMOUSE.md"), "patch": patch})
    assert out.startswith("REFUSED"), out
    assert (ws / "DOURMOUSE.md").read_text() == _OWNER_TEXT + "\n"


def test_apply_patch_cannot_edit_it(ws):
    (ws / "DOURMOUSE.md").write_text(_OWNER_TEXT + "\n")
    diff = (
        "--- a/DOURMOUSE.md\n+++ b/DOURMOUSE.md\n@@ -1 +1 @@\n"
        f"-{_OWNER_TEXT}\n+{_MARKER}\n"
    )
    out = system_access._apply_patch_tool({"path": str(ws / "DOURMOUSE.md"), "diff": diff})
    assert out.startswith("REFUSED"), out
    assert (ws / "DOURMOUSE.md").read_text() == _OWNER_TEXT + "\n"
    assert _MARKER not in _prompt()


def test_the_refusal_names_the_reason_and_the_gate_does_not_ask_instead(ws):
    out = system_access._write_path_tool({"path": str(ws / "DOURMOUSE.md"), "content": _MARKER})
    assert "DOURMOUSE.md" in out and "standing instructions" in out
    # The gate leaves a refused path to the tool, so no approval card can turn it into a write.
    assert system_access.write_gate("write_path", {"path": str(ws / "DOURMOUSE.md")}) is None


def test_other_markdown_in_the_workspace_is_still_writable(ws):
    out = system_access._write_path_tool({"path": str(ws / "notes" / "dourmouse_ideas.md"), "content": "hi"})
    assert out.startswith("WROTE"), out
