"""FIX-W2 H-FD1-5: orch_net's meta file and experience log, and mcp_client's server list, are never
replaced by an empty value after a failed read, and a malformed MCP config is reported, not silently {}."""
from __future__ import annotations

import json
import logging
import os

import pytest

from dourmouse import mcp_client, orch_net
from dourmouse.orch_net import NeuroStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_NET", "1")
    monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
    orch_net._STORES.clear()
    orch_net._MODEL_CACHE.clear()
    s = orch_net.open_store()
    for i in range(3):
        s.log_experience({"prompt": f"task {i}", "ts": f"2026-08-09T10:0{i}:00", "tools_used": [],
                          "agents_used": [], "outcome_ok": True, "session_stem": "s1"})
    yield s
    orch_net._STORES.clear()
    orch_net._MODEL_CACHE.clear()


def _aside(s):
    return [p.name for p in s.base.iterdir() if ".corrupt-" in p.name]


def test_a_corrupt_meta_file_is_kept_aside_when_the_next_write_replaces_it(store):
    store.meta_path.write_text("{not json", encoding="utf-8")
    assert store.apply_feedback("s1", "good") == 3
    assert json.loads(store.meta_path.read_text())["dirty"] is True
    kept = [p for p in store.base.iterdir() if ".corrupt-" in p.name]
    assert kept and kept[0].read_text() == "{not json"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_meta_file_is_left_exactly_as_it_is(store):
    store._write_meta({"trained_count": 7, "agent_names": ["a"], "dirty": False})
    raw = store.meta_path.read_text()
    os.chmod(store.meta_path, 0)
    try:
        assert store.apply_feedback("s1", "bad") == 3  # the feedback itself is still recorded
    finally:
        os.chmod(store.meta_path, 0o600)
    assert store.meta_path.read_text() == raw
    assert _aside(store) == []


def test_a_good_meta_keeps_its_other_keys_through_a_feedback_write(store):
    store._write_meta({"trained_count": 7, "agent_names": ["a", "b"], "dirty": False})
    store.apply_feedback("s1", "good")
    meta = json.loads(store.meta_path.read_text())
    assert meta["trained_count"] == 7 and meta["agent_names"] == ["a", "b"] and meta["dirty"] is True


def test_meta_write_is_atomic_with_no_temp_left(store):
    store._write_meta({"x": 1})
    assert [p.name for p in store.base.iterdir() if p.name.endswith(".tmp") or ".meta.json." in p.name] == []


def test_feedback_rewrite_keeps_a_copy_of_lines_it_could_not_parse(store):
    with store.experiences_path.open("a") as fh:
        fh.write("{half written\n")
    store._seen = None
    store.apply_feedback("s1", "good")
    kept = [p for p in store.base.iterdir() if p.name.startswith("experiences.jsonl.corrupt-")]
    assert kept and "{half written" in kept[0].read_text()
    assert "{half written" not in store.experiences_path.read_text()


def test_a_clean_feedback_rewrite_makes_no_copy(store):
    store.apply_feedback("s1", "good")
    assert [p for p in store.base.iterdir() if ".corrupt-" in p.name] == []


# -- mcp_client -------------------------------------------------------------- #

def test_a_malformed_mcp_config_is_reported_and_left_alone(tmp_path, caplog):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert mcp_client.load_external_mcp_servers(cfg) == {}
    assert any("mcp.json" in r.getMessage() and "could not be read" in r.getMessage() for r in caplog.records)
    assert cfg.read_text(encoding="utf-8") == "{broken"


def test_a_missing_mcp_config_is_quiet(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert mcp_client.load_external_mcp_servers(tmp_path / "none.json") == {}
    assert caplog.records == []


def test_a_config_without_a_servers_map_is_reported(tmp_path, caplog):
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": 5}', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert mcp_client.load_external_mcp_servers(cfg) == {}
    assert any("mcpServers" in r.getMessage() for r in caplog.records)


def test_a_good_config_loads(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {"a": {"command": "x"}}}', encoding="utf-8")
    assert mcp_client.load_external_mcp_servers(cfg) == {"a": {"command": "x"}}


def test_nothing_in_the_repo_writes_the_mcp_config_the_loader_reads():
    """The 'return {} then write' pattern needs a writer; this loader has none (the owner hand-edits it)."""
    import re
    from pathlib import Path

    root = Path(mcp_client.__file__).parent
    writers = [p.name for p in root.glob("*.py") if p.name != "mcp_client.py"
               and re.search(r"default_config\(\).*(write_text|atomic_write)", p.read_text(encoding="utf-8"), re.S)]
    assert writers == []
