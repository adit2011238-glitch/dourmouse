"""FIX-W2 W2R-6: a state file that is merely unreadable right now (permission, I/O) is NOT corrupt:
it must never be renamed away. Only a file that was read and is bad JSON or the wrong shape is set aside."""
from __future__ import annotations

import json
import os

import pytest

from dourmouse import bulk_ingest, live_feeds, project_bookkeeper, usage_tracker
from dourmouse.state_file import StateUnreadable, load_json_or_aside

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


@pytest.fixture
def locked(tmp_path):
    """A valid JSON file made unreadable with chmod 000; restored afterwards."""
    made = []

    def make(name, value):
        p = tmp_path / name
        p.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(p, 0)
        made.append(p)
        return p

    yield make
    for p in made:
        os.chmod(p, 0o600)


def _siblings(path):
    return sorted(q.name for q in path.parent.iterdir() if q.name != path.name and ".lock" not in q.name)


def test_an_unreadable_file_is_left_in_place_and_reported_distinctly(locked):
    p = locked("tasks.json", [{"id": "task-1"}])
    with pytest.raises(StateUnreadable) as exc:
        load_json_or_aside(p, list)
    assert "left untouched" in str(exc.value) and isinstance(exc.value, OSError)
    assert p.exists() and _siblings(p) == []


def test_bad_json_is_still_set_aside(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text("{not json", encoding="utf-8")
    value, note = load_json_or_aside(p, list)
    assert value is None and "kept as tasks.json.corrupt-" in note
    assert not p.exists()


def test_the_wrong_shape_is_still_set_aside(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    value, note = load_json_or_aside(p, list)
    assert value is None and "not a list" in note and not p.exists()


def test_invalid_utf8_bytes_are_corrupt_not_unreadable(tmp_path):
    p = tmp_path / "x.json"
    p.write_bytes(b"\xff\xfe\x00bad")
    value, note = load_json_or_aside(p, dict)
    assert value is None and "kept as" in note


def test_live_feeds_add_task_refuses_and_keeps_the_file_when_it_is_unreadable(locked, monkeypatch):
    p = locked("tasks.json", [{"id": "task-1", "title": "keep", "created_at": "", "done": False}])
    monkeypatch.setattr(live_feeds, "_tasks_path", lambda: p)
    with pytest.raises(RuntimeError) as exc:
        live_feeds.add_task("new")
    assert "not changed" in str(exc.value) or "untouched" in str(exc.value)
    assert p.exists() and _siblings(p) == []


def test_bookkeeper_keeps_an_unreadable_store_and_refuses_writes(locked):
    p = locked("bookkeeper.json", {"projects": {"a": {}}})
    store = project_bookkeeper._load_store(p)
    assert store.get("_unwritable") is True and p.exists() and _siblings(p) == []
    with pytest.raises(OSError):
        project_bookkeeper._save_store(store, p)


def test_usage_tracker_update_refuses_and_keeps_an_unreadable_file(locked):
    p = locked("usage.json", {"total": 1})
    with pytest.raises(OSError):
        usage_tracker._load_for_update(p)
    assert p.exists() and _siblings(p) == []


def test_bulk_ingest_checkpoint_unreadable_stops_the_run_without_touching_it(locked):
    p = locked("ckpt.json", ["a", "b"])
    with pytest.raises(OSError):
        bulk_ingest._load_checkpoint(p)
    assert p.exists() and _siblings(p) == []
