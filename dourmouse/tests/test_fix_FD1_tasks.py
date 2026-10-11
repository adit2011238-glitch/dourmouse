"""Fix agent FD1: P4-32, an unreadable tasks.json is never replaced by an empty list; writes are atomic, locked,
and ids do not collide."""

from __future__ import annotations

import json
import threading

import pytest

from dourmouse import live_feeds


@pytest.fixture(autouse=True)
def _tasks_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_TASKS_FILE", str(tmp_path / "tasks.json"))
    return tmp_path / "tasks.json"


def test_truncated_file_is_kept_and_add_task_reports_instead_of_overwriting(_tasks_file, tmp_path):
    good = json.dumps([{"id": "task-1", "title": "real", "created_at": "x", "done": False}])
    _tasks_file.write_text(good[:-5])  # truncated by a crash
    with pytest.raises(RuntimeError, match="kept as"):
        live_feeds.add_task("new one")
    kept = list(tmp_path.glob("tasks.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text() == good[:-5]
    # nothing was written over it, and a retry now starts a fresh list
    assert live_feeds.add_task("new one")["id"] == "task-1"
    assert kept[0].exists()


def test_listing_an_unreadable_file_raises_and_does_not_touch_it(_tasks_file):
    _tasks_file.write_text("{not json")
    with pytest.raises(RuntimeError, match="could not be read"):
        live_feeds.list_tasks()
    assert _tasks_file.read_text() == "{not json"


def test_a_json_object_instead_of_a_list_is_treated_as_unreadable(_tasks_file):
    _tasks_file.write_text('{"a": 1}')
    with pytest.raises(RuntimeError):
        live_feeds.complete_task("task-1")
    assert not _tasks_file.exists() or _tasks_file.read_text() != "[]\n"


def test_complete_task_on_unreadable_file_does_not_wipe_it(_tasks_file, tmp_path):
    _tasks_file.write_text("[{")
    with pytest.raises(RuntimeError):
        live_feeds.complete_task("task-1")
    assert any(p.read_text() == "[{" for p in tmp_path.glob("tasks.json*"))


def test_ids_come_from_the_maximum_not_the_length(_tasks_file):
    _tasks_file.write_text(json.dumps([
        {"id": "task-1", "title": "a", "created_at": "1", "done": True},
        {"id": "task-5", "title": "b", "created_at": "2", "done": False},
    ]))
    assert live_feeds.add_task("c")["id"] == "task-6"


def test_concurrent_adds_neither_collide_nor_lose_updates():
    errs = []

    def work(i):
        try:
            live_feeds.add_task(f"t{i}")
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errs
    tasks = live_feeds.list_tasks()
    assert len(tasks) == 20
    assert len({t["id"] for t in tasks}) == 20


def test_save_is_atomic_no_temp_files_left(_tasks_file, tmp_path):
    live_feeds.add_task("x")
    assert json.loads(_tasks_file.read_text())[0]["title"] == "x"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
