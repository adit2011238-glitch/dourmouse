"""Hermetic tests for the TradingView <-> dourmouse bridge (v8.4).

No network, no TradingView: exercises parse -> validate -> persist against
a temp workspace, plus the Pine/alert template generators.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from dourmouse import tradingview_ops as tv


@pytest.fixture(autouse=True)
def _tmp_workspace(monkeypatch, tmp_path):
    """Point the signals log at a temp dir for every test."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    yield tmp_path


def _form_payload(signal: dict) -> bytes:
    import urllib.parse

    return urllib.parse.urlencode({"payload": json.dumps(signal)}).encode()


class TestParse:
    def test_json_body(self):
        sig = {"ticker": "HE1!", "side": "buy"}
        out = tv.parse_tv_payload(json.dumps(sig).encode(), "application/json")
        assert out == sig

    def test_form_encoded_payload(self):
        sig = {"ticker": "ZC1!", "side": "sell"}
        out = tv.parse_tv_payload(_form_payload(sig), "application/x-www-form-urlencoded")
        assert out == sig

    def test_garbage_returns_none(self):
        assert tv.parse_tv_payload(b"not json at all", "application/json") is None
        assert tv.parse_tv_payload(b"", "application/json") is None

    def test_non_dict_json_returns_none(self):
        assert tv.parse_tv_payload(b'["a"]', "application/json") is None


class TestValidate:
    def test_open_webhook_accepts(self):
        assert tv.validate_signal({"ticker": "HE1!"}) == (True, "ok")

    def test_secret_required_when_configured(self, monkeypatch):
        monkeypatch.setenv("TV_WEBHOOK_SECRET", "s3cret")
        ok, reason = tv.validate_signal({"ticker": "HE1!", "secret": "wrong"})
        assert not ok and "secret" in reason
        ok, reason = tv.validate_signal({"ticker": "HE1!", "secret": "s3cret"})
        assert ok

    def test_missing_ticker_rejected(self):
        ok, reason = tv.validate_signal({"side": "buy"})
        assert not ok and "ticker" in reason


class TestRecord:
    def test_records_and_strips_secret(self, monkeypatch):
        monkeypatch.setenv("TV_WEBHOOK_SECRET", "k")
        signal = {"secret": "k", "ticker": "HE1!", "side": "sell", "strategy": "HE_8"}
        record = tv.record_signal(signal)
        assert "secret" not in record
        assert record["ticker"] == "HE1!"
        assert record["source"] == "tradingview"
        assert "received_utc" in record
        rows = tv.recent_signals()
        assert len(rows) == 1 and rows[0]["ticker"] == "HE1!"

    def test_recent_signals_empty_when_no_log(self):
        assert tv.recent_signals() == []


class TestWebhook:
    def test_full_pipeline_ok(self, tmp_path):
        sig = {"ticker": "ZC1!", "side": "buy", "strategy": "ZC_12", "price": "450.5"}
        result = tv.handle_tv_webhook(_form_payload(sig), "application/x-www-form-urlencoded")
        assert result["ok"] is True
        assert result["record"]["strategy"] == "ZC_12"
        log = tmp_path / "tv_signals.jsonl"
        assert log.exists() and log.read_text(encoding="utf-8").count("\n") == 1

    def test_bad_body_ok_false(self):
        result = tv.handle_tv_webhook(b"garbage", "application/json")
        assert result["ok"] is False


class TestTemplates:
    def test_pine_script_generated(self):
        src = tv.tv_pine_script("HE_8")
        assert src is not None
        assert "TARGET_MONTH = 8" in src
        assert 'strategy.entry("HE_8-S", strategy.short)' in src
        assert "HE_8" in src

    def test_pine_long_variant(self):
        src = tv.tv_pine_script("ZC_12")
        assert 'strategy.entry("ZC_12-L", strategy.long)' in src

    def test_unknown_leg_none(self):
        assert tv.tv_pine_script("NOPE") is None

    def test_alert_template_has_secret_and_placeholders(self, monkeypatch):
        monkeypatch.setenv("TV_WEBHOOK_SECRET", "abc")
        msg = tv.tv_alert_template("HE_8")
        assert msg is not None
        parsed = json.loads(msg)
        assert parsed["secret"] == "abc"
        assert "{{ticker}}" in parsed["ticker"]
        assert parsed["strategy"] == "HE_8"

    def test_alert_template_unknown_leg_none(self):
        assert tv.tv_alert_template("NOPE") is None


class TestPaperRouting:
    """Signals must drive the seasonal paper log (open/close semantics)."""

    def _make_paper_dir(self, tmp_path):
        p = tmp_path / "fxdata" / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p / "paper_log.csv"

    def test_open_short_then_close_long(self, tmp_path, monkeypatch):
        import csv

        log = self._make_paper_dir(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        # HE_8 is short: sell = open, buy = close
        r1 = tv.route_to_paper({"strategy": "HE_8", "side": "sell", "price": "92.5"})
        assert r1["applied"] and r1["action"] == "open"
        rows = list(csv.reader(log.read_text(encoding="utf-8").splitlines()))
        assert rows[1][0] == "HE_8" and rows[1][1] == "short"
        assert rows[1][6] == ""  # no exit yet
        # close at a higher price -> short loses money
        r2 = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "95.0"})
        assert r2["applied"] and r2["action"] == "close"
        assert r2["pnl_pct"] < 0  # entry 92.5 -> exit 95 on a short = loss
        rows = list(csv.reader(log.read_text(encoding="utf-8").splitlines()))
        assert rows[1][6] != "" and float(rows[1][9]) < 0

    def test_long_leg_buy_open_sell_close(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        (tmp_path / "fxdata" / "reports").mkdir(parents=True, exist_ok=True)
        r1 = tv.route_to_paper({"strategy": "ZC_12", "side": "buy", "price": "450"})
        assert r1["applied"] and r1["action"] == "open"
        r2 = tv.route_to_paper({"strategy": "ZC_12", "side": "sell", "price": "465"})
        assert r2["applied"] and r2["action"] == "close"
        assert r2["pnl_pct"] > 0  # long up = profit

    def test_unknown_leg_not_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        r = tv.route_to_paper({"strategy": "NOPE", "side": "buy", "price": "1"})
        assert not r["applied"] and r["action"] == "unknown"

    def test_bad_price_not_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        r = tv.route_to_paper({"strategy": "HE_8", "side": "sell", "price": "abc"})
        assert not r["applied"]

    def test_close_without_open_not_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        r = tv.route_to_paper({"strategy": "HE_8", "side": "buy", "price": "92"})
        assert not r["applied"] and "no open" in r.get("reason", "")

    def test_webhook_applies_paper(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
        sig = {"ticker": "ZC1!", "side": "buy", "strategy": "ZC_12", "price": "450.5"}
        result = tv.handle_tv_webhook(_form_payload(sig), "application/x-www-form-urlencoded")
        assert result["ok"] is True
        assert result["paper"]["applied"] is True
        assert result["paper"]["action"] == "open"

    def test_webhook_honest_without_paper_path(self, tmp_path, monkeypatch):
        # No FOREX_DATA_PATH -> the signal still logs, routing is honest.
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        sig = {"ticker": "ZC1!", "side": "buy", "strategy": "ZC_12", "price": "450.5"}
        result = tv.handle_tv_webhook(_form_payload(sig), "application/x-www-form-urlencoded")
        assert result["ok"] is True
        assert result["paper"]["applied"] is False
        assert "FOREX_DATA_PATH" in result["paper"].get("reason", "")


class TestPanel:
    def test_snapshot_shape(self, tmp_path):
        snap = tv.tv_panel_snapshot()
        assert snap["configured"] is False  # no secret configured in tests
        assert {l["key"] for l in snap["legs"]} == {"HE_4", "HE_8", "ZC_12"}
        assert snap["recent_signals"] == []
