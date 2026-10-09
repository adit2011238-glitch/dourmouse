"""FR fixes P3-43 and P3-44 in orch_net.py."""

from __future__ import annotations

import json
import threading

import numpy as np
import pytest

from dourmouse import orch_net
from dourmouse.orch_net import NeuroStore, OrchNet


@pytest.fixture()
def net_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_NET", "1")
    monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
    orch_net._STORES.clear()
    orch_net._MODEL_CACHE.clear()
    yield tmp_path / "neuro"
    orch_net._STORES.clear()
    orch_net._MODEL_CACHE.clear()


def _log(store, n, start=0, tools=None, agents=None):
    for i in range(start, start + n):
        store.log_experience({
            "prompt": f"please do task number {i}", "ts": f"2026-08-09T10:{i % 60:02d}:{i // 60:02d}",
            "tools_used": tools or [], "agents_used": agents or [], "outcome_ok": True,
            "session_stem": f"s{i % 3}",
        })


# ---- P3-43 -----------------------------------------------------------------

def test_open_store_is_one_instance_per_directory_and_does_not_reparse(net_env, monkeypatch):
    first = orch_net.open_store()
    _log(first, 5)
    parses = []
    real = NeuroStore._read_records
    monkeypatch.setattr(NeuroStore, "_read_records", lambda self: parses.append(1) or real(self))
    for _ in range(50):
        store = orch_net.open_store()
        store.count()
    assert store is first
    assert parses == []  # the hot path never re-reads the log


def test_a_corrupt_line_is_skipped_not_fatal(net_env):
    store = orch_net.open_store()
    _log(store, 3)
    with store.experiences_path.open("a") as fh:
        fh.write('{"id": "half writ')  # a crash mid-append
        fh.write("\n")
    assert len(store.load_experiences()) == 3
    fresh = NeuroStore(net_env)  # a new process reading the same file
    assert fresh.count() == 3
    assert store.log_experience({"prompt": "after the bad line", "ts": "2026-08-10T00:00:00"})
    assert len(store.load_experiences()) == 4


def test_changes_made_outside_the_instance_are_noticed(net_env):
    store = orch_net.open_store()
    _log(store, 2)
    other = NeuroStore(net_env)  # e.g. another process appended
    _log(other, 3, start=100)
    assert store.count() == 5


def test_appends_and_a_feedback_rewrite_never_lose_records(net_env):
    store = orch_net.open_store()
    _log(store, 10)
    errors = []

    def appender(base):
        try:
            _log(orch_net.open_store(), 40, start=base)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def rater():
        try:
            for _ in range(15):
                orch_net.open_store().apply_feedback("s1", "good")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=appender, args=(1000 * (i + 1),)) for i in range(3)] + [threading.Thread(target=rater)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.load_experiences()) == 10 + 3 * 40
    assert not list(net_env.glob("*.tmp"))


# ---- P3-44 -----------------------------------------------------------------

def _trained(net_env, agents):
    store = orch_net.open_store()
    for i in range(30):
        a = agents[i % len(agents)]
        store.log_experience({
            "prompt": f"{a} job number {i}", "ts": f"2026-08-09T10:{i:02d}:00",
            "tools_used": ["t1", "t2"] if i % 2 else [], "agents_used": [a], "outcome_ok": True,
        })
    store.train(agents)
    return store


def test_agent_vocabulary_is_stored_inside_the_weights_file(net_env):
    store = _trained(net_env, ["mail", "docs"])
    assert OrchNet.saved_agent_names(store.weights_path) == ["mail", "docs"]


def test_a_stale_meta_cannot_pair_new_weights_with_an_old_vocabulary(net_env):
    store = _trained(net_env, ["mail", "docs", "news"])
    meta = json.loads(store.meta_path.read_text())
    meta["agent_names"] = ["mail"]  # what a reader between the two writes used to see
    store.meta_path.write_text(json.dumps(meta))
    orch_net._MODEL_CACHE.clear()
    loaded = orch_net._load_active_model()
    assert loaded is not None
    _net, used_meta = loaded
    assert used_meta["agent_names"] == ["mail", "docs", "news"]
    scores = orch_net.neural_agent_scores("news job number 3", ["mail", "docs", "news"])
    assert set(scores) == {"mail", "docs", "news"}


def test_weights_without_stored_names_and_a_mismatched_meta_are_not_used(net_env):
    store = _trained(net_env, ["mail", "docs", "news"])
    data = dict(np.load(store.weights_path))
    data.pop("agent_names")
    np.savez(store.weights_path, **data)  # an older file: names only in meta
    meta = json.loads(store.meta_path.read_text())
    meta["agent_names"] = ["mail"]
    store.meta_path.write_text(json.dumps(meta))
    orch_net._MODEL_CACHE.clear()
    assert orch_net._load_active_model() is None  # no IndexError, no wrong rows
    assert orch_net.neural_agent_scores("x", ["mail"]) is None
