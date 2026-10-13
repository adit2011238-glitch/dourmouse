"""PRIV stage 1: the privacy data model (tiers, placeholders, consent store, egress ledger).

PRIV-1 (tiers and consent state), PRIV-2 (personal identifiers beyond secrets), PRIV-4 (the ledger
file itself). The dispatch wiring is tested in test_fix_PRIV_gate.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dourmouse import privacy
from dourmouse.governance import PiiRedactor, iban_valid, luhn_valid


# --------------------------------------------------------------------------- #
# PRIV-2: reversible placeholders
# --------------------------------------------------------------------------- #

def _redact(text: str, owner: set[str] | None = None) -> tuple[str, dict[str, int], privacy.PlaceholderMap]:
    mapping = privacy.PlaceholderMap()
    out, counts = PiiRedactor(owner_emails=owner or set()).redact(text, mapping)
    return out, counts, mapping


def test_checksums():
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert iban_valid("GB82WEST12345698765432")
    assert not iban_valid("GB82WEST12345698765433")


def test_card_with_luhn_is_replaced_and_a_failing_number_is_not():
    out, counts, _ = _redact("pay with 4111 1111 1111 1111 not 4111 1111 1111 1112")
    assert "4111 1111 1111 1111" not in out
    assert "[CARD_1]" in out
    assert "4111 1111 1111 1112" in out
    assert counts == {"CARD": 1}


def test_timestamp_shaped_numbers_are_not_cards():
    out, counts, _ = _redact("created 1697040000000 and id 1234567890123")
    assert out == "created 1697040000000 and id 1234567890123"
    assert counts == {}


def test_iban_with_checksum():
    out, counts, _ = _redact("IBAN GB82 WEST 1234 5698 7654 32 and fake GB82 WEST 1234 5698 7654 33")
    assert "[IBAN_1]" in out
    assert "GB82 WEST 1234 5698 7654 32" not in out
    assert "GB82 WEST 1234 5698 7654 33" in out
    assert counts.get("IBAN") == 1


def test_government_ids():
    out, counts, _ = _redact("SSN 123-45-6789, NI AB 12 34 56 C, PAN ABCPE1234F, passport no: X1234567")
    for raw in ("123-45-6789", "AB 12 34 56 C", "ABCPE1234F", "X1234567"):
        assert raw not in out
    assert counts.get("ID") == 4


def test_passport_word_without_a_number_is_left_alone():
    out, counts, _ = _redact("passport photo requirements for travel")
    assert out == "passport photo requirements for travel"
    assert counts == {}


def test_phone_numbers_and_dates():
    out, counts, _ = _redact("call +44 20 7946 0958 or (415) 555-0132 or 020 7946 0958; meeting on 2024-10-12 at 10:30")
    assert "+44 20 7946 0958" not in out and "(415) 555-0132" not in out and "020 7946 0958" not in out
    assert "2024-10-12" in out and "10:30" in out
    assert counts.get("PHONE") == 3


def test_street_address():
    out, counts, _ = _redact("Ship it to 221B Baker Street, London please")
    assert "221B Baker Street" not in out
    assert "[ADDRESS_1]" in out
    assert counts == {"ADDRESS": 1}


def test_owner_email_is_kept_other_people_are_replaced():
    out, counts, _ = _redact("From: Alice <alice@example.org> To: me@owner.test", owner={"me@owner.test"})
    assert "alice@example.org" not in out
    assert "me@owner.test" in out
    assert counts == {"EMAIL": 1}


def test_same_value_same_placeholder_and_restore():
    out, _, mapping = _redact("alice@example.org wrote to bob@example.org, cc alice@example.org")
    assert out == "[EMAIL_1] wrote to [EMAIL_2], cc [EMAIL_1]"
    assert mapping.restore_text(out) == "alice@example.org wrote to bob@example.org, cc alice@example.org"


def test_cards_are_not_restored_into_tool_arguments():
    _, _, mapping = _redact("card 4111 1111 1111 1111 for alice@example.org")
    args = {"to": "[EMAIL_1]", "body": "use [CARD_1]", "nested": ["[EMAIL_1]"]}
    # FIX-P R5-5: only recipient fields of the mail tools get real values back (the old test pinned "everywhere").
    restored = mapping.restore_arguments(args, "gmail_send")
    assert restored == {"to": "alice@example.org", "body": "use [CARD_1]", "nested": ["[EMAIL_1]"]}
    assert mapping.restore_arguments({"body": "to [EMAIL_1] card [CARD_1]"}, "write_note") == {
        "body": "to alice@example.org card [CARD_1]"}
    # The owner's reply text gets everything back.
    assert mapping.restore_text("[CARD_1]") == "4111 1111 1111 1111"


def test_unknown_placeholder_is_left_as_is():
    mapping = privacy.PlaceholderMap()
    assert mapping.restore_text("[EMAIL_9] stays") == "[EMAIL_9] stays"


def test_stream_restorer_handles_a_split_placeholder():
    _, _, mapping = _redact("alice@example.org")
    got: list[str] = []
    r = privacy.StreamRestorer(mapping, got.append)
    for chunk in ("Write to [EM", "AIL_1", "] today", " [not a placeholder"):
        r.feed(chunk)
    r.finish()
    assert "".join(got) == "Write to alice@example.org today [not a placeholder"


def test_maps_are_per_chat_and_bounded():
    a = privacy.placeholder_map("chat-a")
    b = privacy.placeholder_map("chat-b")
    assert a is privacy.placeholder_map("chat-a")
    assert a is not b


# --------------------------------------------------------------------------- #
# PRIV-1: tiers
# --------------------------------------------------------------------------- #

def test_tier_of_public_personal_sensitive_tools():
    assert privacy.classify("web_search", {}) == ("T0", None)
    assert privacy.classify("list_calendar_events", {}) == ("T1", None)
    assert privacy.classify("gmail_read", {}) == ("T2", "mail")
    assert privacy.classify("recall", {}) == ("T2", "memory")
    assert privacy.classify("drive_read", {}) == ("T2", "documents")
    assert privacy.classify("t212_positions", {}) == ("T2", "finance")
    assert privacy.classify("browser_snapshot", {}) == ("T2", "screen")
    assert privacy.classify("run_command", {"command": "ls"}) == ("T2", "files")


def test_unlisted_tool_takes_its_agent_default():
    assert privacy.classify("some_new_mail_tool", {}, agent="mail") == ("T2", "mail")
    assert privacy.classify("some_new_tool", {}, agent="research_info") == ("T1", None)


def test_path_tools_depend_on_the_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    assert privacy.classify("read_path", {"path": str(ws / "notes.txt")}) == ("T1", None)
    assert privacy.classify("read_path", {"path": str(tmp_path / "elsewhere.txt")}) == ("T2", "files")


@pytest.mark.parametrize("arguments", [
    {"path": "~/.ssh/id_rsa"},
    {"path": "/Users/me/project/.env"},
    {"path": "/Users/me/Library/Keychains/login.keychain-db"},
    {"path": "/Users/me/Library/Application Support/Google/Chrome/Default/Cookies"},
    {"command": "cat ~/.aws/credentials"},
    {"command": "security find-generic-password -s github -w"},
])
def test_t3_by_path_or_command(arguments):
    assert privacy.t3_reason("read_path" if "path" in arguments else "run_command", arguments, "anything")


def test_t3_by_content():
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    assert privacy.t3_reason("read_path", {"path": "/tmp/x.txt"}, key)
    assert privacy.t3_reason("read_path", {"path": "/tmp/c.txt"}, "# Netscape HTTP Cookie File\n.example.com\tTRUE")
    privacy.register_never_send("S3cr3t-owner-value-that-is-long-enough-123")
    assert privacy.t3_reason("run_command", {"command": "echo"}, "x S3cr3t-owner-value-that-is-long-enough-123 y")
    assert privacy.t3_reason("read_path", {"path": "/tmp/plain.txt"}, "just a normal file") is None


def test_system_folders_are_not_t3():
    assert privacy.t3_reason("run_command", {"command": "ls /usr/bin"}, "python3") is None
    assert privacy.t3_reason("read_path", {"path": "/etc/hosts"}, "127.0.0.1 localhost") is None


# --------------------------------------------------------------------------- #
# PRIV-1 / PRIV-5: consent and Private mode state
# --------------------------------------------------------------------------- #

def test_consent_defaults_to_ask_and_round_trips():
    store = privacy.PrivacyStore()
    assert store.consent("mail") is None
    store.set_consent("mail", "allow")
    store.set_consent("memory", "deny")
    fresh = privacy.PrivacyStore()
    assert fresh.consent("mail") == "allow"
    assert fresh.consent("memory") == "deny"
    store.set_consent("mail", "ask")
    assert privacy.PrivacyStore().consent("mail") is None


def test_consent_rejects_unknown_source_and_value():
    store = privacy.PrivacyStore()
    with pytest.raises(ValueError):
        store.set_consent("bank_vault", "allow")
    with pytest.raises(ValueError):
        store.set_consent("mail", "maybe")


def test_corrupt_store_counts_as_ask_and_is_kept_aside():
    path = privacy.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    store = privacy.PrivacyStore()
    assert store.consent("mail") is None
    assert not path.exists() or path.read_text(encoding="utf-8") != "{not json"
    assert any(p.name.startswith("privacy.json.corrupt-") for p in path.parent.iterdir())


def test_private_mode_global_and_per_chat():
    store = privacy.PrivacyStore()
    assert not store.private_for("tab-1")
    store.set_chat_private("tab-1", True)
    assert store.private_for("tab-1")
    assert not store.private_for("tab-2")
    store.set_private_mode(True)
    assert store.private_for("tab-2")
    store.set_private_mode(False)
    store.set_chat_private("tab-1", False)
    assert not privacy.PrivacyStore().private_for("tab-1")


def test_provider_verified_flag():
    store = privacy.PrivacyStore()
    store.set_provider_verified("ollama_cloud", True)
    rows = {r["id"]: r for r in privacy.provider_table(store)}
    assert rows["ollama_cloud"]["verified"] is True
    assert rows["gemini"]["verified"] is False
    with pytest.raises(ValueError):
        store.set_provider_verified("not_a_backend", True)


# --------------------------------------------------------------------------- #
# PRIV-4: the egress ledger file
# --------------------------------------------------------------------------- #

def test_egress_row_has_metadata_and_never_content():
    secret_body = "Dear Bob, the merger closes on Friday"
    privacy.record_egress(backend="ollama_cloud", model="gpt-oss:120b",
                          messages=[{"role": "user", "content": secret_body}])
    rows = privacy.read_egress(10)
    assert len(rows) == 1
    row = rows[0]
    for key in ("ts", "provider", "backend", "model", "bytes", "messages", "tiers", "sources", "redactions", "sha256"):
        assert key in row
    assert row["provider"].startswith("Ollama")
    raw = privacy.egress_path().read_text(encoding="utf-8")
    assert "merger" not in raw and "Bob" not in raw


def test_egress_ledger_rotates(monkeypatch):
    monkeypatch.setattr(privacy, "EGRESS_MAX_BYTES", 600)
    for _ in range(30):
        privacy.record_egress(backend="gemini", model="m", text="x" * 50)
    path = privacy.egress_path()
    assert path.stat().st_size <= 600 + 400
    assert Path(str(path) + ".1").exists()
    assert not Path(str(path) + ".4").exists()
    assert len(privacy.read_egress(500)) > 0


def test_egress_rows_carry_the_turn_metadata():
    with privacy.turn_scope("tab-x") as turn:
        turn.tiers["T2"] += 2
        turn.sources.add("mail")
        turn.redactions["EMAIL"] += 3
        privacy.record_egress(backend="claude_cli", model="claude", text="hello")
    row = privacy.read_egress(1)[0]
    assert row["tiers"]["T2"] == 2
    assert row["sources"] == ["mail"]
    assert row["redactions"] == {"EMAIL": 3}
    assert row["chat"] and row["chat"] != "tab-x"
    assert json.dumps(row)
