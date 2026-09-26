"""Finding #140: a damaged history file never stops the app from starting."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from dourmouse.db_recovery import is_damage, open_with_recovery
from dourmouse.general_roster import build_general_registry
from dourmouse.office_logger import OfficeLogger


class TestOpenWithRecovery:
    def test_a_file_that_is_not_a_database_is_set_aside_and_replaced(self, tmp_path):
        path = tmp_path / "office_log.db"
        path.write_bytes(b"this is not sqlite" * 100)
        log = open_with_recovery(lambda: OfficeLogger(path), path, "office log")
        assert log.recent_messages(limit=1) == []  # a fresh, working store
        kept = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes().startswith(b"this is not sqlite")

    def test_a_healthy_file_is_left_alone(self, tmp_path):
        path = tmp_path / "office_log.db"
        OfficeLogger(path).log_message({"id": "1", "from": "a", "to": "b", "subject": "s", "body": "x"})
        log = open_with_recovery(lambda: OfficeLogger(path), path, "office log")
        assert len(log.recent_messages(limit=5)) == 1
        assert not [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]

    def test_a_locked_database_is_never_moved(self, tmp_path):
        path = tmp_path / "office_log.db"
        OfficeLogger(path)

        def busy() -> OfficeLogger:
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_with_recovery(busy, path, "office log")
        assert path.exists() and not [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]

    def test_only_damage_counts(self):
        assert is_damage(sqlite3.DatabaseError("file is not a database"))
        assert is_damage(sqlite3.DatabaseError("database disk image is malformed"))
        assert not is_damage(sqlite3.OperationalError("database is locked"))
        assert not is_damage(ValueError("file is not a database"))

    def test_a_second_failure_is_not_hidden(self, tmp_path):
        path = tmp_path / "office_log.db"
        path.write_bytes(b"junk" * 50)

        def always_broken() -> OfficeLogger:
            raise sqlite3.DatabaseError("file is not a database")

        with pytest.raises(sqlite3.DatabaseError):
            open_with_recovery(always_broken, path, "office log")


def test_the_server_starts_with_a_damaged_office_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    log_path = tmp_path / "ws" / "office" / "office_log.db"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"\x00garbage" * 200)
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert srv.office_log.recent_messages(limit=1) == []
        assert any(".corrupt-" in p.name for p in log_path.parent.iterdir())
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
