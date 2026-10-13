"""FIX-P R5-14: PiiRedactor false positives corrupt ordinary text; real identifiers still swap."""

from __future__ import annotations

import pytest

from dourmouse import privacy
from dourmouse.governance import PiiRedactor


def _redact(text: str) -> str:
    return PiiRedactor(frozenset()).redact(text, privacy.PlaceholderMap())[0]


@pytest.mark.parametrize("text", [
    "host 192.168.100.1234 is up",
    "id tinder2024ab done",
    "tin 2024-10-12 was the date",
    "build 2024 1012 1230 passed",
    "order 2345 6789 0123 4567 shipped",
    "tracking 4111 1111 1111 1112 and order id 4242424242424242",
    "remote git@github.com:owner/repo.git cloned",
    "see 10.20.30.4567 and 255.255.255.1234",
])
def test_ordinary_text_is_left_alone(text):
    assert _redact(text) == text


@pytest.mark.parametrize("text,gone", [
    ("pay 4111 1111 1111 1111 now", "4111 1111 1111 1111"),
    ("amex 3782 822463 10005 expires", "3782 822463 10005"),
    ("call 415-555-0132.", "415-555-0132"),
    ("call +44 20 7946 0958", "+44 20 7946 0958"),
    ("ssn 123-45-6789", "123-45-6789"),
    ("passport: X1234567 valid", "X1234567"),
    ("tax id 123456789", "123456789"),
    ("Aadhaar number 2345 6789 0123", "2345 6789 0123"),
    ("mail carol@example.org", "carol@example.org"),
    ("deliver to 221B Baker Street", "221B Baker Street"),
    ("PAN ABCPE1234F", "ABCPE1234F"),
])
def test_real_identifiers_still_swap(text, gone):
    out = _redact(text)
    assert gone not in out and "[" in out, out
