"""Tests for the scheduled-task entrypoint (dourmouse/history_sync_cli.py).

A scheduled task that fails loudly on a transient condition (no config
yet, a network miss) is worse than one that quietly reports and waits
for the next run — Windows Task Scheduler surfaces a nonzero exit as a
failure notification for something that isn't actually broken. Every
path here must return 0.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from dourmouse import history_sync_cli
from dourmouse.memory_store import MemoryStoreUnavailable


class TestMain:
    def test_not_configured_exits_zero(self, capsys):
        with patch("dourmouse.history_sync_cli.MemoryStore") as MockStore, \
             patch("dourmouse.history_sync_cli.sync_and_import") as mock_sync:
            MockStore.return_value = MagicMock()
            mock_sync.return_value = {"ok": False, "reason": "not_configured"}
            rc = history_sync_cli.main()
        assert rc == 0
        assert "not set" in capsys.readouterr().out.lower()

    def test_unreachable_remote_exits_zero_not_a_failure(self, capsys):
        """A scheduled task retries next time -- a transient network miss
        must never look like a broken task to Windows Task Scheduler."""
        with patch("dourmouse.history_sync_cli.MemoryStore") as MockStore, \
             patch("dourmouse.history_sync_cli.sync_and_import") as mock_sync:
            MockStore.return_value = MagicMock()
            mock_sync.return_value = {"ok": False, "reason": "remote_unreachable"}
            rc = history_sync_cli.main()
        assert rc == 0
        assert "retry" in capsys.readouterr().out.lower()

    def test_successful_sync_exits_zero_and_summarizes(self, capsys):
        with patch("dourmouse.history_sync_cli.MemoryStore") as MockStore, \
             patch("dourmouse.history_sync_cli.sync_and_import") as mock_sync:
            MockStore.return_value = MagicMock()
            mock_sync.return_value = {
                "ok": True, "pulled": 3,
                "claude": {"imported": 2, "scanned": 5},
                "codex": {"imported": 1, "scanned": 1},
            }
            rc = history_sync_cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "2 imported" in out
        assert "1 imported" in out

    def test_memory_store_unavailable_exits_zero(self, capsys):
        """A machine without SQLite FTS5 must not fail the scheduled
        task -- same honesty convention as every other Store & Learn
        surface in this codebase (Rule 2.2)."""
        with patch("dourmouse.history_sync_cli.MemoryStore",
                    side_effect=MemoryStoreUnavailable("no FTS5")):
            rc = history_sync_cli.main()
        assert rc == 0
        assert "not configured" in capsys.readouterr().out.lower()

    def test_store_is_always_closed(self):
        with patch("dourmouse.history_sync_cli.MemoryStore") as MockStore, \
             patch("dourmouse.history_sync_cli.sync_and_import") as mock_sync:
            mock_store = MagicMock()
            MockStore.return_value = mock_store
            mock_sync.return_value = {"ok": False, "reason": "not_configured"}
            history_sync_cli.main()
        mock_store.close.assert_called_once()

    def test_store_is_closed_even_when_sync_raises(self):
        with patch("dourmouse.history_sync_cli.MemoryStore") as MockStore, \
             patch("dourmouse.history_sync_cli.sync_and_import", side_effect=RuntimeError("boom")):
            mock_store = MagicMock()
            MockStore.return_value = mock_store
            try:
                history_sync_cli.main()
            except RuntimeError:
                pass
        mock_store.close.assert_called_once()
