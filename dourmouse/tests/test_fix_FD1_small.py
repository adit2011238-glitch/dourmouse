"""Fix agent FD1: small independent fixes P5-4 (all_hands), P3-1 (artifacts), P3-7 (bench)."""

from __future__ import annotations

import json

import pytest

from dourmouse import all_hands, artifacts, bench


@pytest.mark.parametrize("exc", [ValueError("bad task path"), OSError("spawn failed"), KeyError("cfg")])
def test_backend_slash_reports_any_failure_as_an_error_card(monkeypatch, exc):
    def boom(text, force_backend=None):
        raise exc

    monkeypatch.setattr(all_hands, "_default_brain", boom)
    out = all_hands._run_backend_slash("claude", "do a thing")
    assert out["ok"] is False and out["text"]


def test_publish_artifact_accepts_a_capitalised_kind_with_json_content(tmp_path, monkeypatch):
    store = artifacts.ArtifactStore()
    monkeypatch.setattr(artifacts, "default_store", lambda: store)
    content = json.dumps({"columns": ["a"], "rows": [[1]]})
    for kind in ("Table", "TABLE", " table "):
        out = artifacts.publish_artifact_tool({"kind": kind, "title": "t", "content": content})
        assert out.startswith("ARTIFACT PUBLISHED"), out


def test_percentile_is_nearest_rank():
    assert bench.percentile(list(range(1, 11)), 50) == 5
    assert bench.percentile([1, 2], 50) == 1
    assert bench.percentile(list(range(1, 11)), 70) == 7  # 0.7*10 is 7.000000000000001 in floats
    assert bench.percentile(list(range(1, 21)), 95) == 19
    assert bench.percentile([5], 95) == 5
    assert bench.percentile(list(range(1, 11)), 100) == 10
    assert bench.percentile(list(range(1, 11)), 1) == 1
