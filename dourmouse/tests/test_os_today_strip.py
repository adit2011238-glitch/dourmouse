"""Today strip (plan 4.12, PL-08): six cards from one route, each failing on its own, plus Continue and the privacy line."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import projects_store as ps
from dourmouse.os_api import Request
from dourmouse.os_api import today as t

_HELPERS = Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "home" / "helpers.js"


def req(**server):
    return Request(server=SimpleNamespace(**server), user=None)


def test_a_card_that_raises_or_hangs_is_that_cards_error_only(monkeypatch):
    monkeypatch.setattr(t, "mail_card", lambda r: (_ for _ in ()).throw(RuntimeError("imap down")))
    monkeypatch.setattr(t, "calendar_card", lambda r, n: (time.sleep(2), t._card("calendar", "ok", "late"))[1])
    monkeypatch.setattr(t, "overnight_card", lambda r, n=None: t._card("overnight", "empty", "Quiet overnight.", 0))
    monkeypatch.setattr(t, "security_card", lambda r: t._card("security", "empty", "No scan.", None))
    monkeypatch.setattr(t, "usage_card", lambda: t._card("usage", "empty", "none", 0))
    cards = {c["key"]: c for c in t.strip(req(), {"running": [], "needs_you": []}, seconds=0.3)}
    assert list(cards) == list(t.CARD_ORDER)
    assert cards["mail"]["state"] == "error" and "imap down" in cards["mail"]["line"]
    assert cards["calendar"]["state"] == "error" and "too long" in cards["calendar"]["line"]
    assert cards["goals"]["state"] == "empty" and cards["usage"]["state"] == "empty"


def test_goals_card_counts_running_and_waiting_and_says_when_it_cannot_read():
    c = t.goals_card({"running": [{}, {}], "needs_you": [{}]})
    assert c["number"] == 3 and c["line"] == "2 running, 1 waiting for you." and c["action"]["target"] == "goals"
    assert t.goals_card({})["state"] == "empty"
    broken = t.strip(req(), None, seconds=2)
    assert next(x for x in broken if x["key"] == "goals")["state"] == "error"


def test_security_card_reads_the_last_scan_and_reports_no_scan_or_a_failed_one():
    finding = lambda sev: SimpleNamespace(severity=sev)  # noqa: E731
    sentry = SimpleNamespace(last_result=SimpleNamespace(all_findings=[finding("high"), finding("med"), finding("low")]),
                             last_scan_at=1.0, consecutive_failed_scans=0, last_scan_error=None)
    c = t.security_card(req(security_sentry=sentry))
    assert c["number"] == 1 and c["line"] == "1 high finding, 1 medium."
    assert t.security_card(req())["state"] == "empty"
    sentry.consecutive_failed_scans, sentry.last_scan_error = 2, "lsof timed out"
    c = t.security_card(req(security_sentry=sentry))
    assert c["state"] == "error" and "lsof timed out" in c["line"]


def test_usage_card_uses_the_persisted_totals(monkeypatch):
    from dourmouse import usage_tracker

    monkeypatch.setattr(usage_tracker, "get_totals", lambda: {
        "claude": {"requests": 3, "cost_usd": 1.5}, "ollama": {"requests": 2, "prompt_tokens": 1000, "completion_tokens": 234}})
    c = t.usage_card()
    assert c["number"] == 5 and "$1.50" in c["line"] and "1,234 tokens" in c["line"]
    monkeypatch.setattr(usage_tracker, "get_totals", lambda: {"claude": {}, "ollama": {}})
    assert t.usage_card()["state"] == "empty"


def test_overnight_counts_runs_since_22_00_and_failures(monkeypatch, tmp_path):
    from dourmouse import automation_runs as ar

    now = datetime(2026, 11, 20, 8, 0).astimezone()
    since = t.overnight_window(now)
    assert datetime.fromtimestamp(since).hour == 22 and since < now.timestamp()
    rows = [
        {"ts": since + 60, "status": "done"}, {"ts": since + 120, "status": "failed"},
        {"ts": since - 3600, "status": "done"},  # before quiet hours began
        {"ts": since + 200, "status": "running"},  # still going, not an outcome
    ]
    monkeypatch.setattr(ar.RunHistory, "_load", lambda self: rows)
    watcher = SimpleNamespace(recent=lambda limit, important_only: [{"ts": since + 10}, {"ts": since - 10}])
    c = t.overnight_card(req(news_watcher=watcher), now)
    assert c["number"] == 3 and "2 automation runs, 1 failed" in c["line"] and "1 important news item" in c["line"]
    monkeypatch.setattr(ar.RunHistory, "_load", lambda self: [])
    assert t.overnight_card(req(), now)["state"] == "empty"


def test_mail_card_states(monkeypatch):
    from dourmouse.os_api import comms

    def payload(**kw):
        return lambda r: (200, {"ok": True, "cached_at": 100.0, **kw})

    monkeypatch.setattr(comms, "inbox", payload(state="unavailable", rows=[]))
    assert t.mail_card(req())["state"] == "unavailable"
    rows = [{"from": '"Ms Carter" <c@x.y>', "subject": "Essay draft", "unread": True}, {"from": "b", "subject": "s", "unread": False}]
    monkeypatch.setattr(comms, "inbox", payload(state="populated", rows=rows))
    c = t.mail_card(req())
    assert c["number"] == 1 and "Ms Carter" in c["line"] and c["at"] == 100.0
    monkeypatch.setattr(comms, "inbox", payload(state="populated", rows=[{"from": "a", "subject": "s", "unread": None}]))
    assert t.mail_card(req())["number"] is None  # unread is unknown on this path: no invented figure
    monkeypatch.setattr(comms, "inbox", payload(state="populated", rows=[{"unread": False}]))
    assert t.mail_card(req())["state"] == "empty"

    def boom(r):
        raise comms.ApiError(502, "IMAP refused")

    monkeypatch.setattr(comms, "inbox", boom)
    assert t.mail_card(req())["state"] == "error"


def test_calendar_card_picks_the_next_event(monkeypatch):
    cal = {"state": "ok", "events": [{"time": "08:00", "title": "Gym"}, {"time": "13:30", "title": "Chemistry"}], "more": 1, "note": ""}
    c = t._calendar_card(cal, datetime(2026, 11, 20, 9, 0).astimezone())
    assert c["number"] == 3 and c["line"] == "Next at 13:30: Chemistry"
    assert t._calendar_card({"state": "signed_out", "events": [], "note": "Sign in"}, None)["state"] == "unavailable"


def test_privacy_line_counts_only_the_last_24_hours(monkeypatch):
    from dourmouse import privacy

    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    stamp = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rows = [
        {"ts": stamp(1), "sources": ["a", "b"], "redactions": {"EMAIL": 2}},
        {"ts": stamp(23), "sources": [], "redactions": {}},
        {"ts": stamp(30), "sources": ["old"], "redactions": {"EMAIL": 9}},
        {"ts": "garbage"},
    ]
    monkeypatch.setattr(privacy, "read_egress", lambda limit=100: rows)
    assert t.privacy_line(now.timestamp()) == {"hours": 24, "requests": 2, "sources": 2, "redactions": 2}


def test_continue_is_up_to_three_newest_threads(tmp_path):
    ps.set_project_store(ps.ProjectStore(tmp_path))
    try:
        store = ps.get_project_store()
        a, b = store.create("Alpha"), store.create("Beta")
        for i in range(2):
            th = store.new_thread(a["id"], f"A{i}")
            store.record_turn(a["id"], th["id"], "q", "a")
        th = store.new_thread(b["id"], "B0")
        store.record_turn(b["id"], th["id"], "q", "a")
        items = t.continue_items()
        assert len(items) == 3 and items[0]["title"] == "B0" and items[0]["project_name"] == "Beta"
        assert items[0]["target"] == f"projects/{b['id']}/{th['id']}"
    finally:
        ps.set_project_store(None)


def _node(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    # helpers.js imports ../../kit/format.js, which is a sibling module on disk
    script = Path(_HELPERS).parent / "_strip_probe.mjs"
    script.write_text(f"import * as h from './helpers.js';\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    try:
        proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30, check=False)
    finally:
        script.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_strip_helpers_word_age_numbers_and_privacy():
    out = _node("""
const now = Date.parse('2026-11-20T10:00:00Z');
R.ages = [now - 10e3, now - 5 * 60e3, now - 3 * 3600e3, now - 2 * 86400e3].map((t) => h.dataAge(t, now));
R.bad = h.dataAge('nope', now);
R.nums = [h.cardNumber({ state: 'ok', number: 0 }), h.cardNumber({ state: 'ok', number: null }), h.cardNumber({ state: 'error', number: 4 }), h.cardNumber({ state: 'unavailable', number: 1 })];
R.order = h.orderedStrip([{ key: 'usage' }, { key: 'mail' }, { key: 'goals' }]).map((c) => c.key);
R.priv = h.privacyText({ hours: 24, requests: 1, sources: 0, redactions: 2 });
R.none = h.privacyText(null);
""")
    assert out["ages"] == ["just now", "5 min ago", "3 h ago", "2 d ago"] and out["bad"] == ""
    assert out["nums"] == ["0", "–", "–", "–"]
    assert out["order"] == ["mail", "goals", "usage"]
    assert out["priv"] == "Last 24 hours: 1 request to cloud models, 0 sources shared, 2 redactions." and out["none"] == ""
