"""Fix agent FD1: P3-16, email_send_via_smtp always answers with its documented text."""

from __future__ import annotations

import smtplib

import pytest

from dourmouse import email_identity as ei


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("DOURMOUSE_SMTP_USER", "d@example.com")
    monkeypatch.setenv("DOURMOUSE_SMTP_PASS", "pw")
    monkeypatch.setenv("DOURMOUSE_SMTP_FROM", "d@example.com")
    monkeypatch.delenv("DOURMOUSE_SMTP_PORT", raising=False)


def test_connection_failure_is_a_documented_failure_string(monkeypatch):
    def boom(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    out = ei.email_send_via_smtp("a@b.com", "hi", "body")
    assert out.startswith("EMAIL OWN SEND FAILED") and "Nothing was sent" in out


def test_dns_failure_is_a_documented_failure_string(monkeypatch):
    import socket

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    assert ei.email_send_via_smtp("a@b.com", "hi", "body").startswith("EMAIL OWN SEND FAILED")


def test_newline_in_subject_is_refused_not_raised():
    out = ei.email_send_via_smtp("a@b.com", "hi\nBcc: x@y.com", "body")
    assert out.startswith("EMAIL OWN SEND FAILED") and "Nothing was sent" in out


def test_bad_port_value_is_a_documented_failure_string(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_SMTP_PORT", "five-eighty-seven")
    out = ei.email_send_via_smtp("a@b.com", "hi", "body")
    assert out.startswith("EMAIL OWN SEND FAILED") and "Nothing was sent" in out
