"""FS2 P5-53 / P5-54: dismissing a finding asks first; privacy mode withholds every evidence tool."""

from __future__ import annotations

import pytest

from dourmouse import dispatch
from dourmouse.dispatch import Permission
from dourmouse.security import privacy
from dourmouse.security import tools as sec_tools
from dourmouse.security.sentry import SentryFinding, SentryStore


@pytest.fixture(autouse=True)
def _ws(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()


def _tool(name):
    return next(t for t in sec_tools.build_security_subagent().tools if t.name == name)


def _seed():
    store = SentryStore(sec_tools._sentry_db())
    store.record_and_classify(SentryFinding("fp1", "arp_gateway_duplicate", "high", "Router MAC claimed twice", "d", "a"), 1.0)
    return store


def test_dismiss_is_gated_and_the_prompt_quotes_the_finding_title():
    _seed()
    t = _tool("security_sentry_dismiss")
    assert t.permission == Permission.REQUIRES_CONFIRMATION
    prompt = t.confirm_prompt({"fingerprint": "fp1"})
    assert "Router MAC claimed twice" in prompt and "fp1" in prompt


def test_dismiss_prompt_for_an_unknown_fingerprint_does_not_crash():
    t = _tool("security_sentry_dismiss")
    assert "nobody" in t.confirm_prompt({"fingerprint": "nobody"})


def test_declining_the_gate_leaves_the_finding_active():
    store = _seed()
    out = dispatch._execute_tool(_tool("security_sentry_dismiss"), {"fingerprint": "fp1"}, lambda prompt: False)
    assert not out.startswith("Marked")
    assert store.snapshot()[0]["dismissed_false_positive"] == 0


def test_approving_the_gate_dismisses():
    store = _seed()
    out = dispatch._execute_tool(_tool("security_sentry_dismiss"), {"fingerprint": "fp1"}, lambda prompt: True)
    assert out.startswith("Marked")
    assert store.snapshot()[0]["dismissed_false_positive"] == 1


def test_closing_an_incident_already_asks_through_the_dispatcher():
    store = _seed()
    store.open_incident("fp1", "", now=2.0)
    asked = []
    out = dispatch._execute_tool(_tool("security_incident_update"), {"fingerprint": "fp1", "status": "RESOLVED"},
                                 lambda prompt: asked.append(prompt) or False)
    assert asked and store.get_incident("fp1")["status"] == "OPEN"
    assert not out.startswith("Incident fp1 updated")


@pytest.mark.parametrize("name", ["security_self_audit", "lockdown_status"])
def test_privacy_mode_withholds_self_audit_and_lockdown_status(name):
    privacy.set_privacy_mode(True)
    out = _tool(name).handler({})
    assert out.startswith("Withheld in privacy mode")


def test_privacy_off_still_runs_the_tools():
    privacy.set_privacy_mode(False)
    assert not _tool("lockdown_status").handler({}).startswith("Withheld")


def test_dismiss_asks_exactly_once_through_the_dispatcher():
    _seed()
    prompts = []
    dispatch._execute_tool(_tool("security_sentry_dismiss"), {"fingerprint": "fp1"}, lambda p: prompts.append(p) or False)
    assert len(prompts) == 1 and "Router MAC claimed twice" in prompts[0]
