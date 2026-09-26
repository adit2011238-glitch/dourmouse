"""The rules the lockdown browser extension installs (extension_rules)."""

from __future__ import annotations

import json

from dourmouse.security import extension_rules as er
from dourmouse.security import lockdown as ld


def _bl(active=True, *urls):
    bl = ld.Blocklist(active=active)
    for u in urls:
        bl.add_url(u)
    return bl


def test_rules_are_dense_sorted_anchored_blocks():
    rules = er.build_rules(_bl(True, "reddit.com/r/all", "example.com/feed", "https://www.a.com/x?q=1"))
    assert [r["id"] for r in rules] == [1, 2, 3]
    assert [r["condition"]["urlFilter"] for r in rules] == ["||a.com/x", "||example.com/feed", "||reddit.com/r/all"]
    for r in rules:
        assert r["action"] == {"type": "block"}
        assert r["condition"]["resourceTypes"] == ["main_frame", "sub_frame"]


def test_no_rules_unless_lockdown_is_active():
    assert er.build_rules(_bl(False, "reddit.com/r/all")) == []
    assert er.rules_payload(_bl(False, "reddit.com/r/all"))["active"] is False


def test_order_of_adding_does_not_change_the_rules_or_version():
    a = er.rules_payload(_bl(True, "a.com/x", "b.com/y"))
    b = er.rules_payload(_bl(True, "b.com/y", "a.com/x"))
    assert a == b


def test_version_changes_with_the_rules_and_is_stable_otherwise():
    v1 = er.rules_payload(_bl(True, "a.com/x"))["version"]
    assert v1 == er.rules_payload(_bl(True, "a.com/x"))["version"]
    assert v1 != er.rules_payload(_bl(True, "a.com/x", "b.com/y"))["version"]
    assert v1 != er.rules_payload(_bl(False, "a.com/x"))["version"]


def test_payload_shape_is_json_and_far_below_chromes_limit():
    bl = _bl(True, *[f"example.com/p{i}" for i in range(ld.MAX_URLS)])
    payload = er.rules_payload(bl)
    assert set(payload) == {"active", "version", "rules"}
    assert len(json.loads(json.dumps(payload))["rules"]) == ld.MAX_URLS < 30000
    assert isinstance(payload["version"], str) and payload["version"]


def test_whole_site_entries_are_not_extension_rules():
    bl = _bl(True, "a.com/x")
    bl.add_site("reddit.com")
    assert [r["condition"]["urlFilter"] for r in er.build_rules(bl)] == ["||a.com/x"]
