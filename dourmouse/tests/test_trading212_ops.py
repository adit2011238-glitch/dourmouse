"""v8.2 Trading 212 broker tests (trading212_ops.py + roster wiring).

Hermetic (Rule 2.1): no real network — HTTP is monkeypatched at the
urllib boundary, so the account/position/order functions are exercised
against fake T212-shaped responses. Verifies:

- honest NOT CONFIGURED without T212_API_KEY (Rule 2.2)
- demo is the default env; live is double-gated (T212_ALLOW_LIVE=1)
- t212_order refuses without paper_confirm=true (paper-first, Rule 2.1)
- real payload construction (ticker/quantity/type + optional prices)
- the roster carries the t212 subagent with all five tools
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import trading212_ops
from dourmouse.general_roster import build_general_registry
from dourmouse.trading212_ops import (
    T212NotConfiguredError,
    t212_account,
    t212_order,
    t212_portfolio,
    t212_positions,
    t212_status,
)


class _FakeResponse:
    """Context-manager response: ``with urlopen(...) as resp`` works."""

    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_response(payload: dict) -> SimpleNamespace:
    return _FakeResponse(payload)


def _monkey_http(monkeypatch, payload: dict):
    """Route every urlopen to a canned JSON response; capture requests."""

    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "body": req.data.decode() if req.data else None,
            }
        )
        return _fake_response(payload)

    monkeypatch.setattr(trading212_ops.urllib.request, "urlopen", fake_urlopen)
    return captured


def _set_key(monkeypatch, env: str = "demo"):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_ENV", env)
    monkeypatch.delenv("T212_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("T212_CONFIRM_LIVE", raising=False)


class TestConfig:
    def test_missing_key_is_honest(self, monkeypatch):
        monkeypatch.delenv("T212_API_KEY", raising=False)
        with pytest.raises(T212NotConfiguredError):
            trading212_ops._config()
        assert t212_status()["configured"] is False

    def test_bad_env_raises(self, monkeypatch):
        _set_key(monkeypatch)
        monkeypatch.setenv("T212_ENV", "nope")
        with pytest.raises(T212NotConfiguredError):
            trading212_ops._config()

    def test_live_is_double_gated(self, monkeypatch):
        _set_key(monkeypatch, env="live")
        with pytest.raises(T212NotConfiguredError, match="double gate|gated"):
            trading212_ops._config()
        monkeypatch.setenv("T212_ALLOW_LIVE", "1")
        base, key = trading212_ops._config()
        assert base == "https://live.trading212.com"
        assert key == "test-key"

    def test_demo_default(self, monkeypatch):
        _set_key(monkeypatch, env="demo")
        base, _ = trading212_ops._config()
        assert base == "https://demo.trading212.com"

    def test_per_env_demo_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("T212_API_KEY", "fallback-key")
        monkeypatch.setenv("T212_API_KEY_DEMO", "demo-key")
        monkeypatch.setenv("T212_ENV", "demo")
        base, key = trading212_ops._config()
        assert base == "https://demo.trading212.com"
        assert key == "demo-key"

    def test_per_env_live_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("T212_API_KEY", "fallback-key")
        monkeypatch.setenv("T212_API_KEY_LIVE", "live-key")
        monkeypatch.setenv("T212_ENV", "live")
        monkeypatch.setenv("T212_ALLOW_LIVE", "1")
        base, key = trading212_ops._config()
        assert base == "https://live.trading212.com"
        assert key == "live-key"

    def test_fallback_key_still_works(self, monkeypatch):
        monkeypatch.setenv("T212_API_KEY", "fallback-key")
        monkeypatch.delenv("T212_API_KEY_DEMO", raising=False)
        monkeypatch.setenv("T212_ENV", "demo")
        base, key = trading212_ops._config()
        assert base == "https://demo.trading212.com"
        assert key == "fallback-key"

    def test_demo_does_not_require_live_key(self, monkeypatch):
        monkeypatch.delenv("T212_API_KEY", raising=False)
        monkeypatch.setenv("T212_API_KEY_DEMO", "demo-only")
        monkeypatch.setenv("T212_ENV", "demo")
        base, key = trading212_ops._config()
        assert base == "https://demo.trading212.com"
        assert key == "demo-only"


class TestRealCalls:
    def test_account_hits_real_endpoint(self, monkeypatch):
        _set_key(monkeypatch)
        captured = _monkey_http(
            monkeypatch, {"equity": 1234.5, "cash": {"free": 900.0}}
        )
        out = t212_account()
        assert out["equity"] == 1234.5
        assert captured[0]["url"].endswith("/api/v0/equity/account/info")
        assert captured[0]["method"] == "GET"

    def test_positions(self, monkeypatch):
        _set_key(monkeypatch)
        _monkey_http(monkeypatch, [{"ticker": "AAPL", "quantity": 2}])
        out = t212_positions()
        assert out[0]["ticker"] == "AAPL"

    def test_portfolio_composes(self, monkeypatch):
        _set_key(monkeypatch)
        _monkey_http(monkeypatch, {"ok": True})
        out = t212_portfolio()
        assert set(out) == {"account", "positions", "orders"}

    def test_status_probe_result(self, monkeypatch):
        _set_key(monkeypatch)
        _monkey_http(monkeypatch, {"equity": 500.0})
        out = t212_status()
        assert out["configured"] is True
        assert out["environment"] == "demo"
        assert out["account"]["equity"] == 500.0


class TestOrderPaperFirst:
    def test_refuses_without_confirm(self, monkeypatch):
        _set_key(monkeypatch)
        with pytest.raises(ValueError, match="paper_confirm"):
            t212_order("AAPL", 1)

    def test_market_order_payload(self, monkeypatch):
        _set_key(monkeypatch)
        captured = _monkey_http(monkeypatch, {"id": "ORD-1"})
        out = t212_order("AAPL", 2, paper_confirm=True)
        assert out["id"] == "ORD-1"
        req = captured[0]
        assert req["url"].endswith("/api/v0/equity/orders")
        assert req["method"] == "POST"
        body = json.loads(req["body"])
        assert body == {"ticker": "AAPL", "quantity": 2, "type": "MARKET"}

    def test_limit_order_includes_prices(self, monkeypatch):
        _set_key(monkeypatch)
        captured = _monkey_http(monkeypatch, {"id": "ORD-2"})
        t212_order(
            "VOO", 1, order_type="LIMIT", limit_price=510.0, paper_confirm=True
        )
        body = json.loads(captured[0]["body"])
        assert body["type"] == "LIMIT"
        assert body["limitPrice"] == 510.0

    def test_live_order_requires_final_confirm(self, monkeypatch):
        _set_key(monkeypatch, env="live")
        monkeypatch.setenv("T212_ALLOW_LIVE", "1")
        with pytest.raises(ValueError, match="T212_CONFIRM_LIVE"):
            t212_order("AAPL", 1, paper_confirm=True)
        monkeypatch.setenv("T212_CONFIRM_LIVE", "1")
        _monkey_http(monkeypatch, {"id": "ORD-LIVE"})
        out = t212_order("AAPL", 1, paper_confirm=True)
        assert out["id"] == "ORD-LIVE"


class TestRoster:
    def test_t212_subagent_registered(self):
        registry = build_general_registry()
        sub = registry.get_subagent("t212")
        assert sub is not None
        names = {t.name for t in sub.tools}
        assert {
            "t212_status",
            "t212_account",
            "t212_positions",
            "t212_portfolio",
            "t212_order",
        } <= names
