"""FI1 fixes for dourmouse/gdelt_graph.py: P3-22 (a failed download must not
mark the file processed) and P3-23 (the graph is bounded in size)."""

from __future__ import annotations

import pytest

from dourmouse import gdelt_graph as gg


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    monkeypatch.setattr(gg, "_last_processed_url", None)
    monkeypatch.setattr(gg, "_last_fetch_at", None)
    monkeypatch.setattr(gg, "_last_error", None)
    monkeypatch.setattr(gg, "_GRAPH", gg.KineticGraph())
    monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: "http://example.com/a.gkg.csv.zip")


def _rec(i: int, persons: int = 6) -> gg.GkgRecord:
    return gg.GkgRecord(record_id=f"r{i}", date="d", source="s", url="u",
                        persons=[f"person {i}-{k}" for k in range(persons)])


# --------------------------------------------------------------------------- #
# P3-22
# --------------------------------------------------------------------------- #

def test_failed_download_is_retried_on_the_next_poll(monkeypatch):
    calls = []

    def flaky(url, **k):
        calls.append(url)
        if len(calls) == 1:
            k["errors"].append("download failed: timed out")
            return []
        return [_rec(1)]

    monkeypatch.setattr(gg, "fetch_gkg_records", flaky)
    first = gg.poll_once()
    assert first["ok"] is False
    assert "download failed" in first["error"]
    assert gg._last_processed_url is None
    second = gg.poll_once()
    assert second["fetched"] is True and second["records"] == 1
    assert len(calls) == 2
    assert gg._last_processed_url == "http://example.com/a.gkg.csv.zip"


def test_genuinely_empty_file_is_still_marked_processed(monkeypatch):
    calls = []
    monkeypatch.setattr(gg, "fetch_gkg_records", lambda url, **k: calls.append(url) or [])
    first = gg.poll_once()
    assert first["ok"] is True
    assert gg._last_processed_url == "http://example.com/a.gkg.csv.zip"
    gg.poll_once()
    assert len(calls) == 1


def test_fetch_reports_why_it_failed(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(gg.urllib.request, "urlopen", boom)
    errors: list[str] = []
    assert gg.fetch_gkg_records("http://example.com/x.gkg.csv.zip", errors=errors) == []
    assert errors and "connection reset" in errors[0]


def test_corrupt_zip_is_reported(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"not a zip"

    monkeypatch.setattr(gg.urllib.request, "urlopen", lambda *a, **k: Resp())
    errors: list[str] = []
    assert gg.fetch_gkg_records("http://example.com/x.gkg.csv.zip", errors=errors) == []
    assert errors


# --------------------------------------------------------------------------- #
# P3-23
# --------------------------------------------------------------------------- #

def test_edge_count_is_capped_and_heavy_edges_survive():
    g = gg.KineticGraph(max_nodes=10_000, max_edges=200)
    # one pair seen many times (weight 50) ...
    for _ in range(50):
        g.ingest_record(gg.GkgRecord("h", "d", "s", "u", persons=["alice", "bob"]), now=1.0)
    # ... then lots of one-off pairs
    for i in range(100):
        g.ingest_record(_rec(i), now=2.0 + i)
    g.ingest_records([_rec(1000 + i) for i in range(50)])
    nodes, edges = g.counts()
    assert edges <= 200
    snap = g.snapshot(limit_nodes=10_000)
    heavy = [e for e in snap["edges"] if {e["a"], e["b"]} == {"person:alice", "person:bob"}]
    assert heavy and heavy[0]["weight"] == 50


def test_node_count_is_capped_and_no_edge_points_at_a_missing_node():
    g = gg.KineticGraph(max_nodes=60, max_edges=10_000)
    for i in range(40):
        g.ingest_record(_rec(i), now=float(i))
    nodes, _edges = g.counts()
    assert nodes <= 60
    snap = g.snapshot(limit_nodes=10_000)
    ids = {n["id"] for n in snap["nodes"]}
    assert all(e["a"] in ids and e["b"] in ids for e in snap["edges"])
    with g._lock:
        assert all(a in g._nodes and b in g._nodes for (a, b) in g._edges)


def test_default_graph_has_finite_caps():
    g = gg.KineticGraph()
    assert 0 < g._max_nodes < 10**7
    assert 0 < g._max_edges < 10**7
