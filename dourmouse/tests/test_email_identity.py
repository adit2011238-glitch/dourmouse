"""Hermetic tests for the v5.25 email identity (dourmouse/email_identity.py).

No mail is ever sent here — the SMTP send is exercised only on the honest
NOT CONFIGURED path (no identity set). Sending itself is confirmation-gated
at the roster level and needs real credentials, which tests never fake.
"""

from __future__ import annotations

from dourmouse.dispatch import Permission
from dourmouse.email_identity import (
    display_name,
    email_send_via_smtp,
    identity_status,
    own_address,
    smtp_identity,
)
from dourmouse.general_roster import build_general_registry


class TestIdentity:
    def test_display_name_defaults_to_dourmouse(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_EMAIL_NAME", raising=False)
        assert display_name() == "Dourmouse"

    def test_display_name_custom(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMAIL_NAME", "Adit's Assistant")
        assert display_name() == "Adit's Assistant"

    def test_own_address_uses_plus_alias_of_configured_gmail(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "adit@gmail.com")
        monkeypatch.delenv("DOURMOUSE_EMAIL_ADDRESS", raising=False)
        assert own_address() == "adit+dourmouse@gmail.com"

    def test_own_address_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "adit@gmail.com")
        monkeypatch.setenv("DOURMOUSE_EMAIL_ADDRESS", "dourmouse@example.com")
        assert own_address() == "dourmouse@example.com"

    def test_own_address_empty_without_base(self, monkeypatch):
        import dourmouse.google_services as gs

        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_EMAIL_ADDRESS", raising=False)
        monkeypatch.setattr(gs, "_user", lambda: "")
        assert own_address() == ""

    def test_smtp_identity_requires_full_config(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SMTP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_PASS", raising=False)
        assert smtp_identity() == {}

    def test_smtp_identity_detected(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("DOURMOUSE_SMTP_USER", "dourmouse@example.com")
        monkeypatch.setenv("DOURMOUSE_SMTP_PASS", "secret")
        monkeypatch.delenv("DOURMOUSE_SMTP_PORT", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_FROM", raising=False)
        ident = smtp_identity()
        assert ident["host"] == "smtp.example.com"
        assert ident["from"] == "dourmouse@example.com"

    def test_identity_status_shape(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "adit@gmail.com")
        monkeypatch.delenv("DOURMOUSE_SMTP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_PASS", raising=False)
        s = identity_status()
        assert s["name"] == "Dourmouse"
        assert s["own_address"] == "adit+dourmouse@gmail.com"
        assert s["smtp_identity"] is None
        assert s["alias_note"] is not None  # honest note about the +alias
        assert "configured Gmail account" in s["sender_mode"]

    def test_send_without_identity_reports_not_configured(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SMTP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_PASS", raising=False)
        out = email_send_via_smtp("a@b.com", "hi", "body")
        assert out.startswith("NOT CONFIGURED")
        assert "Nothing was sent" in out

    def test_send_validates_recipient_and_subject(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SMTP_HOST", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_USER", raising=False)
        monkeypatch.delenv("DOURMOUSE_SMTP_PASS", raising=False)
        assert "valid recipient" in email_send_via_smtp("not-an-email", "s", "b")
        assert "requires a subject" in email_send_via_smtp("a@b.com", "", "b")


class TestRosterWiring:
    def test_mail_subagent_has_identity_tools(self):
        registry = build_general_registry()
        sub = registry.get_subagent("mail")
        names = {t.name for t in sub.tools}
        assert {"email_identity_status", "email_own_send"} <= names

    def test_email_own_send_is_confirmation_gated(self):
        registry = build_general_registry()
        sub = registry.get_subagent("mail")
        spec = next(t for t in sub.tools if t.name == "email_own_send")
        assert spec.permission == Permission.REQUIRES_CONFIRMATION
        assert spec.confirm_prompt is not None
        assert "email_own_send" in registry.gated_tool_names
