"""FD2 fixes for supabase_sync: P5-60 (default outbox location, rows the server skipped)."""

from __future__ import annotations

from pathlib import Path

from dourmouse.supabase_sync import SupabaseSync
from dourmouse.tests.test_supabase_sync import _FakeStore, _transport


def test_default_outbox_lives_under_the_user_config_dir_not_the_cwd(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(cfg))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    sync = SupabaseSync(_FakeStore())
    try:
        assert Path(sync.outbox.path).resolve().parent == cfg.resolve()
        assert not (elsewhere / "supabase_outbox.db").exists()
    finally:
        sync.close()


def test_construction_does_not_depend_on_a_writable_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir("/")  # what a Finder or Electron launch has
    sync = SupabaseSync(_FakeStore())  # must not raise sqlite OperationalError
    sync.close()


def _sync(tmp_path, resp):
    return SupabaseSync(
        _FakeStore(), url="https://x.supabase.co", anon_key="k", access_token="t",
        outbox_path=tmp_path / "ob.db", transport=_transport([(200, resp)]),
    )


def test_rows_the_server_skipped_are_not_recorded_as_in_sync(tmp_path):
    sync = _sync(tmp_path, [{"synced": 0, "skipped": 1}])
    sync.queue("s", "t", "local body")
    out = sync.push()
    assert out.skipped == 1
    assert sync.outbox.depth() == 0  # still not retried forever
    assert sync.outbox.state("s", "t") is None  # but NOT claimed to match the cloud
    sync.close()


def test_a_fully_accepted_batch_is_recorded_as_synced(tmp_path):
    sync = _sync(tmp_path, [{"synced": 2, "skipped": 0}])
    sync.queue("s", "a", "A")
    sync.queue("s", "b", "B")
    sync.push()
    assert sync.outbox.state("s", "a") is not None
    assert sync.outbox.state("s", "b") is not None
    sync.close()


def test_a_partly_skipped_batch_records_nothing_it_cannot_prove(tmp_path):
    sync = _sync(tmp_path, [{"synced": 1, "skipped": 1}])
    sync.queue("s", "a", "A")
    sync.queue("s", "b", "B")
    sync.push()
    assert sync.outbox.state("s", "a") is None and sync.outbox.state("s", "b") is None
    sync.close()
