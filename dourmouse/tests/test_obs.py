"""Observability must record everything and break nothing."""

from __future__ import annotations

import json

import pytest

from dourmouse import obs


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("DOURMOUSE_OBS_DISABLED", raising=False)
    return tmp_path


def test_error_log_roundtrips_every_field(_logs_to_tmp):
    obs.log_error(
        source="stock_quote",
        kind="not_found",
        what="a quote for X",
        detail="HTTP 404 from https://example.test/x",
        status=404,
        retryable=False,
        extra={"symbol": "X"},
    )
    (row,) = obs.read_recent("errors.log")
    assert row["source"] == "stock_quote"
    assert row["status"] == 404
    assert row["retryable"] is False
    assert row["extra"] == {"symbol": "X"}
    assert "ts" in row


def test_optional_fields_are_omitted_not_nulled(_logs_to_tmp):
    obs.log_error(source="s", kind="unknown", what="w", detail="d")
    (row,) = obs.read_recent("errors.log")
    assert "status" not in row
    assert "retryable" not in row
    assert "extra" not in row


def test_agent_and_perf_go_to_separate_files(_logs_to_tmp):
    obs.log_agent_call(tool="web_search", agent="research_info", ok=True, duration_ms=12.34)
    obs.log_perf(op="inference", duration_ms=210.5)

    (agent_row,) = obs.read_recent("agents.log")
    (perf_row,) = obs.read_recent("perf.log")

    assert agent_row["tool"] == "web_search"
    assert agent_row["ok"] is True
    assert agent_row["duration_ms"] == 12.3
    assert perf_row["op"] == "inference"
    assert obs.read_recent("errors.log") == []


def test_timed_records_duration_on_success(_logs_to_tmp):
    with obs.timed("fetch", extra={"url": "x"}) as scratch:
        scratch["items"] = 3

    (row,) = obs.read_recent("perf.log")
    assert row["op"] == "fetch"
    assert row["duration_ms"] >= 0
    assert row["extra"]["items"] == 3
    assert row["extra"]["url"] == "x"
    assert row["extra"]["ok"] is True


def test_timed_still_records_when_the_block_raises(_logs_to_tmp):
    """A slow failure must be as visible as a slow success."""
    with pytest.raises(ValueError):
        with obs.timed("fetch"):
            raise ValueError("boom")

    (row,) = obs.read_recent("perf.log")
    assert row["op"] == "fetch"
    assert row["extra"]["ok"] is False


def test_logging_never_raises_on_unwritable_dir(monkeypatch, tmp_path):
    """Observability that can break the request path is worse than none."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    monkeypatch.setenv("DOURMOUSE_LOG_DIR", str(blocker / "sub"))

    obs.log_error(source="s", kind="k", what="w", detail="d")  # must not raise
    assert obs.read_recent("errors.log") == []


def test_unserialisable_payload_is_dropped_not_raised(_logs_to_tmp):
    class Weird:
        def __repr__(self):
            return "<weird>"

    obs.log_error(source="s", kind="k", what="w", detail="d", extra={"o": Weird()})
    rows = obs.read_recent("errors.log")
    # default=str keeps it writable; the contract is only "does not raise".
    assert len(rows) <= 1


def test_disable_switch_silences_all_writes(_logs_to_tmp, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_OBS_DISABLED", "1")
    obs.log_error(source="s", kind="k", what="w", detail="d")
    obs.log_agent_call(tool="t", ok=True)
    obs.log_perf(op="o", duration_ms=1.0)
    assert obs.read_recent("errors.log") == []
    assert obs.read_recent("agents.log") == []


def test_rotation_caps_file_growth(_logs_to_tmp, monkeypatch):
    monkeypatch.setattr(obs, "_MAX_BYTES", 2048)
    for i in range(200):
        obs.log_error(source="s", kind="k", what=f"w{i}", detail="x" * 200)

    live = _logs_to_tmp / "errors.log"
    assert live.exists()
    assert live.stat().st_size < 2048 * 3
    assert (_logs_to_tmp / "errors.log.1").exists()


def test_every_line_is_independently_parseable(_logs_to_tmp):
    for i in range(5):
        obs.log_agent_call(tool=f"t{i}", ok=i % 2 == 0)

    raw = (_logs_to_tmp / "agents.log").read_text().splitlines()
    assert len(raw) == 5
    for line in raw:
        json.loads(line)  # JSONL: each line stands alone
