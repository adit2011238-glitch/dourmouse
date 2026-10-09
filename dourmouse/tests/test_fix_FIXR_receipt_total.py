"""FIX-R R-13: the total is the total, not a "Total savings" or "Total VAT" line printed after it."""

from __future__ import annotations

import pytest

from dourmouse import extract
from dourmouse.tests.test_extract import make_pdf


def _total(tmp_path, lines):
    p = tmp_path / "r.pdf"
    p.write_bytes(make_pdf(lines))
    out = extract.extract_receipt(p)
    return next(ln for ln in out.splitlines() if ln.startswith("- total:"))


@pytest.mark.parametrize("after", [
    "Total savings $3.00", "Total saved $3.00", "Total items $4.00", "Total VAT $2.00", "Total tax $2.00",
    "Total discount $1.50", "Total rewards $0.50", "Total qty 4 $0.00", "Total before tax $10.00",
    "Total excl. VAT $10.00", "Total weight 2.5 kg $1.00",
])
def test_a_line_that_starts_with_total_but_is_something_else_is_not_the_total(tmp_path, after):
    assert _total(tmp_path, ["Coffee $4.00", "Subtotal $10.00", "Tax $2.00", "Total $12.00", after]) == "- total: $12.00"


def test_the_reviewers_receipt(tmp_path):
    assert _total(tmp_path, ["Coffee $4.00", "Subtotal $10.00", "Tax $2.00", "Total $12.00", "Total savings $3.00"]) == "- total: $12.00"


def test_the_last_real_total_still_wins_over_a_running_one(tmp_path):
    assert _total(tmp_path, ["Total $5.00", "More shopping $4.00", "Total $9.00"]) == "- total: $9.00"


def test_total_paid_and_total_due_are_totals(tmp_path):
    assert _total(tmp_path, ["Item $4.00", "Total due $4.00"]) == "- total: $4.00"
    assert _total(tmp_path, ["Item $4.00", "Total paid $4.00"]) == "- total: $4.00"


def test_with_only_a_savings_line_the_total_is_not_taken_from_it_by_label(tmp_path):
    out = _total(tmp_path, ["Shop", "Total savings $3.00"])
    assert out == "- total: $3.00", "falls back to the last amount on the page, as before, not through the 'total' label"
