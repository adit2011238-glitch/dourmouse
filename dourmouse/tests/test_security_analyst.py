"""Finding #107 (MS-9): the AI analyst explains findings and can never
invent one; it wakes once per new set, and a dead model is reported."""

from __future__ import annotations

import json

from dourmouse.security import analyst as an
from dourmouse.security.sentry import SentryFinding, SentryScanResult

FINDINGS = [
    {"kind": "firewall_disabled", "severity": "high", "title": "Application Firewall is disabled", "detail": "off",
     "recommended_action": "turn it on"},
    {"kind": "remote_login_on", "severity": "med", "title": "Remote Login (SSH) is on", "detail": "port 22",
     "recommended_action": "turn it off"},
]


def _model(reply):
    seen = []

    def complete(messages):
        seen.append(messages)
        return reply if isinstance(reply, str) else json.dumps(reply)
    complete.seen = seen
    return complete


def test_points_must_cite_given_findings_and_invented_ones_are_dropped():
    reply = {"summary": "Your Mac accepts logins from the network with no firewall.", "worry": "medium",
             "points": [{"findings": [1, 2], "meaning": "together they expose SSH", "first_step": "firewall on"},
                        {"findings": [7], "meaning": "a rootkit!", "first_step": "panic"},
                        {"findings": [], "meaning": "uncited claim"},
                        {"findings": [True], "meaning": "bool is not a number"}]}
    m = _model("Here you go:\n```json\n" + json.dumps(reply) + "\n```")
    r = an.analyze(FINDINGS, m)
    assert r["ok"] and r["worry"] == "medium" and r["dropped"] == 3
    assert r["points"] == [{"findings": [1, 2], "titles": [FINDINGS[0]["title"], FINDINGS[1]["title"]],
                            "meaning": "together they expose SSH", "first_step": "firewall on"}]
    user = m.seen[0][1]["content"]
    assert "[1] (high) Application Firewall is disabled" in user and "[2] (med)" in user


def test_a_dead_model_is_reported_not_faked():
    def boom(messages):
        raise RuntimeError("401 Unauthorized")
    r = an.analyze(FINDINGS, boom)
    assert not r["ok"] and "401" in r["error"]
    r = an.analyze(FINDINGS, _model("I think everything is fine."))
    assert not r["ok"] and "did not return" in r["error"]


def test_an_unknown_worry_level_is_not_passed_through():
    assert an.analyze(FINDINGS, _model({"summary": "s", "worry": "apocalyptic", "points": []}))["worry"] == "unknown"


def _f(fp, sev):
    return SentryFinding(fingerprint=fp, kind="k", severity=sev, title=fp, detail="d", recommended_action="a")


def _scan(new, all_=None):
    return SentryScanResult(all_findings=all_ or new, new_findings=new, suppressed_false_positives=[],
                            risk_score=0.0, telemetry_available={}, alerts_written=0)


def test_wakes_once_per_new_set_and_is_rate_limited(tmp_path):
    told = []
    m = _model({"summary": "one thing changed", "worry": "low", "points": [{"findings": [1], "meaning": "m"}]})
    a = an.Analyst(complete=m, notify=told.append, folder=tmp_path, min_interval=600)
    assert a.on_scan(_scan([_f("low1", "low")]), now=1000) is None  # low severity never wakes it
    first = a.on_scan(_scan([_f("a", "high")]), now=1000)
    assert first["ok"] and first["woke_for"] == ["a"] and told == ["Security: one thing changed"]
    assert a.on_scan(_scan([_f("a", "high")]), now=5000) is None  # same set again
    assert a.on_scan(_scan([_f("b", "med")]), now=1100) is None  # new, but too soon
    assert a.on_scan(_scan([_f("b", "med")]), now=2000)["ok"]
    assert len(m.seen) == 2 and an.latest(tmp_path)["woke_for"] == ["b"]
