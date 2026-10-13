"""FIX-P R5-12 (profile needs Memory consent, swaps identifiers, owner-only route) and H-PRIV-6 (every request
to a provider gets a ledger row)."""

from __future__ import annotations

import json
import types
from typing import Any

from dourmouse import personality_profile as pp
from dourmouse import privacy, request_guard
from dourmouse.memory_store import MemoryStore


class _Client:
    def __init__(self) -> None:
        self.base_url = "https://integrate.api.nvidia.com/v1"
        self.sent: list[Any] = []
        outer = self
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=outer._create))

    def _create(self, **kw):
        self.sent.append(kw["messages"])
        msg = types.SimpleNamespace(content=json.dumps(
            {"working_style": "a", "preferences": "b", "communication_preferences": "c", "general_interests": "d"}))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])


def _store_with_history(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "m.db")
    store.remember("claude_history",
                   "session", "I emailed carol@example.org about the lease on 221B Baker Street")
    return store


def test_profile_refused_without_memory_consent_or_in_private_mode(tmp_path):
    store = _store_with_history(tmp_path)
    client = _Client()
    out = pp.generate_profile(store, client=client, config=types.SimpleNamespace(model="m", base_url=client.base_url))
    assert out == {"ok": False, "reason": "memory_not_allowed"} and client.sent == []
    privacy.PrivacyStore().set_consent("memory", "allow")
    privacy.PrivacyStore().set_private_mode(True)
    out = pp.generate_profile(store, client=client, config=types.SimpleNamespace(model="m", base_url=client.base_url))
    assert out["reason"] == "memory_not_allowed" and client.sent == []


def test_profile_swaps_identifiers_and_writes_a_ledger_row(tmp_path):
    privacy.PrivacyStore().set_consent("memory", "allow")
    store = _store_with_history(tmp_path)
    client = _Client()
    out = pp.generate_profile(store, client=client, config=types.SimpleNamespace(model="m", base_url=client.base_url))
    assert out.get("ok") is True, out
    sent = json.dumps(client.sent)
    assert "carol@example.org" not in sent and "[EMAIL_" in sent
    rows = [r for r in privacy.read_egress(20) if r["origin"] == "personality_profile"]
    assert rows and rows[0]["backend"] == "nvidia"


def test_profile_route_is_owner_only():
    assert request_guard.is_owner_route("POST", "/api/profile/generate")


def test_other_direct_clients_write_a_row():
    from dourmouse import key_check

    class _KC:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: None))

    key_check.validate_key_live("nvapi-test-0000000000000000000000000000000000",
                                client_factory=lambda k, b: _KC())
    assert any(r["origin"] == "key_check" and r["backend"] == "nvidia" for r in privacy.read_egress(20))
    privacy.record_client_request(_Client(), "m", [{"role": "user", "content": "x"}], "atlas_lab")
    assert any(r["origin"] == "atlas_lab" and r["backend"] == "nvidia" for r in privacy.read_egress(20))
