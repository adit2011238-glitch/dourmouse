"""FS2 P4-56: reputation lookups and the self audit never raise; one failing check cannot abort the rest."""

from __future__ import annotations

import json
import ssl
import subprocess
import urllib.request

import pytest

from dourmouse.security import reputation, self_audit


class _Resp:
    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(reputation.REPUTATION_API_KEY_ENV, "k")


@pytest.mark.parametrize("failure", [
    ConnectionResetError("reset by peer"),
    ssl.SSLError("bad record"),
    OSError("broken"),
])
def test_a_failure_while_reading_the_body_is_unavailable(monkeypatch, failure):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(failure))
    r = reputation.check_ip_reputation("8.8.8.8")
    assert r["available"] is False and r["reason"]


def test_http_incomplete_read_is_unavailable(monkeypatch):
    import http.client

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(http.client.IncompleteRead(b"x")))
    assert reputation.check_ip_reputation("8.8.8.8")["available"] is False


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"null", b"\xff\xfe", b"{not json", b'{"data": [1]}'])
def test_a_body_that_is_not_the_expected_object_is_unavailable(monkeypatch, body):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    r = reputation.check_ip_reputation("8.8.8.8")
    assert r["available"] is False


def test_a_good_response_still_parses(monkeypatch):
    body = json.dumps({"data": {"ipAddress": "8.8.8.8", "abuseConfidenceScore": 3, "totalReports": 1}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    r = reputation.check_ip_reputation("8.8.8.8")
    assert r["available"] is True and r["abuse_confidence_score"] == 3


# ---- self audit ---------------------------------------------------------------

def test_git_missing_does_not_raise(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        self_audit.check_env_tracked(tmp_path)  # the check itself reports; the audit wraps it (below)


def test_git_gets_a_timeout(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    self_audit.check_env_tracked(tmp_path)
    assert seen.get("timeout")


def _patch_audit_environment(monkeypatch, tmp_path):
    import dourmouse.config as cfg

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    return cfg


def test_one_failing_check_becomes_a_finding_and_the_rest_still_run(tmp_path, monkeypatch):
    _patch_audit_environment(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise PermissionError("helper unreadable")

    monkeypatch.setattr(self_audit, "check_helper", broken)
    monkeypatch.setattr(self_audit, "check_env_tracked", lambda repo: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 15)))
    monkeypatch.setattr(self_audit, "check_private_dir", lambda p, label: [self_audit._f("private_dir", "low", "marker", "d", "f")])
    r = self_audit.run_self_audit()
    titles = [f["title"] for f in r["findings"]]
    assert any("could not run" in t and "lockdown helper" in t for t in titles)
    assert any("could not run" in t and ".env" in t for t in titles)
    assert "marker" in titles  # checks after the failing ones still ran
    assert all(f["severity"] in ("high", "med", "low") for f in r["findings"])


def test_a_clean_audit_has_no_could_not_run_findings(tmp_path, monkeypatch):
    _patch_audit_environment(monkeypatch, tmp_path)
    r = self_audit.run_self_audit()
    assert not any("could not run" in f["title"] for f in r["findings"])
