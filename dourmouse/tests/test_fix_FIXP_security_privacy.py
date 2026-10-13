"""FIX-P H-PRIV-7 and R5-19: privacy off is owner-only, the security switch and the global Private mode are one
switch, and the analyst is gated as Files and swaps identifiers."""

from __future__ import annotations

import json
from typing import Any

from dourmouse import privacy, request_guard
from dourmouse.general_roster import build_general_registry
from dourmouse.security import analyst
from dourmouse.security import privacy as sp

FINDING = {"kind": "k", "severity": "high", "title": "Download from carol@example.org",
           "detail": "file /Users/x/Downloads/invoice.pdf from 221B Baker Street"}


def test_no_registered_tool_can_switch_privacy():
    names = {t.name for sub in build_general_registry().all_subagents() for t in sub.tools}
    assert "security_privacy_mode" not in names
    assert request_guard.is_owner_route("POST", "/api/security/action")  # the console route that still can, owner only


def test_one_switch():
    assert sp.privacy_mode() is False
    privacy.PrivacyStore().set_private_mode(True)
    assert sp.privacy_mode() is True  # the global switch counts for the security tools and the analyst
    privacy.PrivacyStore().set_private_mode(False)
    sp.set_privacy_mode(True)
    assert privacy.PrivacyStore().private_mode() is True  # and the security switch sets the global one
    sp.set_privacy_mode(False)
    assert privacy.PrivacyStore().private_mode() is False and sp.privacy_mode() is False


def test_analyst_needs_files_consent_and_swaps_identifiers(monkeypatch):
    sent: list[Any] = []

    def complete(messages):  # a stand-in for the cloud model: records what it was handed
        sent.append(messages)
        return json.dumps({"summary": "Check [EMAIL_1]'s file", "worry": "low", "points": [
            {"findings": [1], "meaning": "from [EMAIL_1]", "first_step": "delete it"}]})

    # a custom `complete` is the test seam and bypasses the default cloud gate; the default path is below
    class _Cfg:
        model = "m"

    monkeypatch.setattr(analyst, "default_complete", lambda: complete)
    out = analyst.analyze([FINDING])
    assert out["ok"] is False and out["skipped"] is True and sent == []  # no Files consent
    privacy.PrivacyStore().set_consent("files", "allow")
    out = analyst.analyze([FINDING])
    assert out["ok"] is True
    wire = json.dumps(sent)
    assert "carol@example.org" not in wire and "[EMAIL_1]" in wire and "221B Baker Street" not in wire
    assert "carol@example.org" in out["summary"] and "carol@example.org" in out["points"][0]["meaning"]
    privacy.PrivacyStore().set_private_mode(True)
    sent.clear()
    assert analyst.analyze([FINDING])["skipped"] is True and sent == []
