"""Dourmouse email identity (v5.25) — the machine's own mail identity.

Two honest layers:

1. **Identity** — who Dourmouse presents AS when it sends mail: a display
   name (``DOURMOUSE_EMAIL_NAME``, default "Dourmouse"), a base address, and
   an **own address**. The own address defaults to ``<base>+dourmouse@...``
   on the configured Gmail account — a real, immediately-working receiving
   alias (mail to it lands in the same inbox) with NO new account. Sending
   FROM that exact alias additionally needs the Gmail "Send mail as" setting
   (one manual click in Gmail settings; the browser agent can drive it).

2. **Dedicated SMTP identity** — once Dourmouse has its own mailbox on ANY
   provider (a ``+dourmouse`` Gmail alias, a domain address, or an account
   the browser agent signed up for), point ``DOURMOUSE_SMTP_HOST / USER /
   PASS`` at it and ``email_own_send`` sends FROM that identity. Until then
   it reports NOT CONFIGURED honestly — never a fabricated send (Rule 2.2).
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any


def display_name() -> str:
    return os.environ.get("DOURMOUSE_EMAIL_NAME", "").strip() or "Dourmouse"


def base_address() -> str:
    """The account Dourmouse currently sends through (Gmail user, else env)."""
    from dourmouse.google_services import _user

    try:
        user = _user()
    except Exception:  # noqa: BLE001 - _user is env-only; never fatal
        user = ""
    return user or os.environ.get("DOURMOUSE_EMAIL_ADDRESS", "").strip()


def own_address() -> str:
    """The address Dourmouse presents as its own.

    ``DOURMOUSE_EMAIL_ADDRESS`` wins when set (the real dedicated address).
    Otherwise the ``+dourmouse`` Gmail alias of the configured account —
    real receiving alias, no new account needed.
    """
    explicit = os.environ.get("DOURMOUSE_EMAIL_ADDRESS", "").strip()
    if explicit:
        return explicit
    base = base_address()
    if base and "@" in base:
        user, _, domain = base.partition("@")
        return f"{user}+dourmouse@{domain}"
    return ""


def smtp_identity() -> dict[str, str]:
    """The dedicated SMTP identity, or {} when not configured."""
    host = os.environ.get("DOURMOUSE_SMTP_HOST", "").strip()
    user = os.environ.get("DOURMOUSE_SMTP_USER", "").strip()
    password = os.environ.get("DOURMOUSE_SMTP_PASS", "").strip()
    if not (host and user and password):
        return {}
    return {
        "host": host,
        "port": os.environ.get("DOURMOUSE_SMTP_PORT", "587").strip(),
        "user": user,
        "tls": os.environ.get("DOURMOUSE_SMTP_TLS", "1").strip(),
        "from": os.environ.get("DOURMOUSE_SMTP_FROM", "").strip() or user,
    }


def identity_status() -> dict[str, Any]:
    smtp = smtp_identity()
    base = base_address()
    own = own_address()
    return {
        "name": display_name(),
        "base_address": base or None,
        "own_address": own or None,
        "alias_note": (
            "The +dourmouse address is a real receiving alias on your Gmail "
            "account (mail to it lands in the same inbox). Sending FROM it "
            "needs the Gmail 'Send mail as' setting — one click in Gmail "
            "settings; the browser agent can drive that flow."
            if own and not smtp and own.endswith("+dourmouse@gmail.com")
            else None
        ),
        "smtp_identity": (
            {"host": smtp["host"], "port": smtp["port"], "from": smtp["from"], "tls": smtp["tls"]}
            if smtp
            else None
        ),
        "sender_mode": (
            "dedicated SMTP identity"
            if smtp
            else "configured Gmail account (sends as the account address)"
        ),
    }


def email_send_via_smtp(to: str, subject: str, body: str) -> str:
    """Send an email FROM the dedicated Dourmouse identity (SMTP).

    Must be confirmation-gated upstream. Reports NOT CONFIGURED honestly when
    the SMTP identity is missing — never a fabricated send.
    """
    to = (to or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not to or "@" not in to:
        return "ERROR: email_own_send requires a valid recipient address."
    if not subject:
        return "ERROR: email_own_send requires a subject."
    smtp = smtp_identity()
    if not smtp:
        return (
            "NOT CONFIGURED: Dourmouse has no dedicated mailbox yet. The "
            "honest paths: (1) zero-setup — the +dourmouse receiving alias on "
            "your Gmail (set DOURMOUSE_SMTP_HOST/USER/PASS to your Gmail "
            "app-password SMTP and DOURMOUSE_SMTP_FROM to "
            "you+dourmouse@gmail.com after enabling 'Send mail as'), or (2) "
            "a real dedicated address — create one (the browser agent can "
            "sign up on a provider you choose) and set DOURMOUSE_SMTP_HOST / "
            "DOURMOUSE_SMTP_USER / DOURMOUSE_SMTP_PASS in .env. Nothing was sent."
        )
    port = int(smtp["port"] or 587)
    tls = smtp["tls"] not in ("0", "false", "False")
    msg = EmailMessage()
    msg["From"] = formataddr((display_name(), smtp["from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body[:50_000])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(smtp["host"], port, timeout=30) as server:
                server.login(smtp["user"], os.environ.get("DOURMOUSE_SMTP_PASS", ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp["host"], port, timeout=30) as server:
                if tls:
                    server.starttls()
                server.login(smtp["user"], os.environ.get("DOURMOUSE_SMTP_PASS", ""))
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        return (
            f"EMAIL OWN SEND FAILED: authentication rejected ({exc.smtp_code}) "
            f"on {smtp['host']}. Check DOURMOUSE_SMTP_USER / "
            "DOURMOUSE_SMTP_PASS. Nothing was sent."
        )
    except smtplib.SMTPException as exc:
        return f"EMAIL OWN SEND FAILED: SMTP error: {exc}. Nothing was sent."
    return (
        f"EMAIL OWN SEND OK: message delivered to {to} from "
        f"{display_name()} <{smtp['from']}> with subject {subject!r}."
    )
