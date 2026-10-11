"""FD2 fix P5-55: a second draft of an already-approved tool must not overwrite or delete it."""

from __future__ import annotations

import hashlib

import pytest

from dourmouse import self_extensions as se

HANDLER_A = "def handle(arguments: dict) -> str:\n    return 'A'\n"
HANDLER_B = "def handle(arguments: dict) -> str:\n    return 'B'\n"
TEST = "def test_it():\n    assert True\n"
SCHEMA = {"type": "object", "properties": {}, "required": []}


@pytest.fixture(autouse=True)
def ws(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv(se._APPROVED_DIR_OVERRIDE_ENV, raising=False)


def _add(handler, name="fetch_x"):
    return se.SelfExtensions().add_draft(
        capability_gap="gap", tool_name=name, description="Does a thing.",
        parameters_schema=SCHEMA, handler_source=handler, test_source=TEST,
    )


def _approve(entry, monkeypatch, passed=True):
    monkeypatch.setattr(se, "_write_and_run_test", lambda e, d: (passed, "out"))
    return se.approve(entry["id"], changelog_path=None)


def test_second_draft_of_an_approved_tool_is_refused_and_first_survives(monkeypatch):
    a = _add(HANDLER_A)
    b = _add(HANDLER_B)
    assert _approve(a, monkeypatch)["ok"] is True
    path = se._approved_dir() / "fetch_x.py"
    before = path.read_bytes()
    res = _approve(b, monkeypatch)
    assert res["ok"] is False
    assert "fetch_x" in res["error"] and a["id"] in res["error"]
    assert se.SelfExtensions().get(b["id"])["status"] == "DRAFTED"  # left for the owner to reject
    assert path.read_bytes() == before
    store = se.SelfExtensions()
    assert store.get(a["id"])["status"] == "APPROVED"
    assert store.approved_hash("fetch_x") == hashlib.sha256(before).hexdigest()


def test_failing_test_leaves_no_module_at_the_final_path(monkeypatch):
    a = _add(HANDLER_A, name="solo_tool")
    res = _approve(a, monkeypatch, passed=False)
    assert res["ok"] is False
    assert not (se._approved_dir() / "solo_tool.py").exists()
    assert se.list_approved_names() == []


def test_module_is_staged_while_the_test_runs_and_promoted_only_on_pass(monkeypatch):
    a = _add(HANDLER_A, name="staged_tool")
    seen = {}

    def fake_test(entry, digest):
        seen["final_exists_during_test"] = (se._approved_dir() / "staged_tool.py").exists()
        return True, "ok"

    monkeypatch.setattr(se, "_write_and_run_test", fake_test)
    res = se.approve(a["id"], changelog_path=None)
    assert res["ok"] is True
    assert seen["final_exists_during_test"] is False
    final = se._approved_dir() / "staged_tool.py"
    assert final.is_file()
    assert hashlib.sha256(final.read_bytes()).hexdigest() == res["entry"]["module_sha256"]
    assert se.list_approved_names() == ["staged_tool"]  # no staging leftovers listed
    assert not list(se._approved_dir().rglob("*.staging*"))


def test_failed_approval_of_another_tool_does_not_touch_an_approved_module(monkeypatch):
    # A failed draft B (different name) must not disturb A's module either.
    a = _add(HANDLER_A, name="tool_aaa")
    b = _add(HANDLER_B, name="tool_bbb")
    assert _approve(a, monkeypatch)["ok"] is True
    assert _approve(b, monkeypatch, passed=False)["ok"] is False
    assert (se._approved_dir() / "tool_aaa.py").is_file()


# --- H-FD2-1: the drafts store itself (same "read fails, write the default" class) --------- #
def test_corrupt_draft_line_is_kept_aside_when_the_store_is_rewritten():
    store = se.SelfExtensions()
    a = _add(HANDLER_A, name="tool_one")
    path = se._drafts_path()
    path.write_text(path.read_text() + '{"id": "ext-002", "status": "DRAFTED", "tool_na\n', encoding="utf-8")
    b = _add(HANDLER_B, name="tool_two")  # rewrites the whole file
    asides = list(path.parent.glob("drafts.jsonl.corrupt-*"))
    assert len(asides) == 1
    assert '"id": "ext-002"' in asides[0].read_text(encoding="utf-8")
    ids = [e["id"] for e in store.list()]
    assert len(ids) == len(set(ids)) == 2  # no id reused after the bad line was dropped
    assert b["id"] != a["id"]


def test_draft_ids_never_collide_after_an_entry_is_lost():
    _add(HANDLER_A, name="tool_one")
    b = _add(HANDLER_B, name="tool_two")
    path = se._drafts_path()
    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n", encoding="utf-8")  # only ext-002 remains
    c = _add(HANDLER_A, name="tool_three")
    assert c["id"] != b["id"]
    assert len({e["id"] for e in se.SelfExtensions().list()}) == 2


def test_drafts_file_is_written_atomically(monkeypatch):
    _add(HANDLER_A, name="tool_one")
    before = se._drafts_path().read_text()

    def boom(src, dst):
        raise OSError("disk full")

    import os

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            _add(HANDLER_B, name="tool_two")
    assert se._drafts_path().read_text() == before
