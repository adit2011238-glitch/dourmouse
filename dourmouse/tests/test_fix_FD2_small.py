"""FD2 fixes: mobile_link (P3-37), net_errors (P5-32), model_check (P3-38)."""

from __future__ import annotations

import io
import stat
import urllib.error

import dourmouse.mobile_link as ml
from dourmouse import model_check
from dourmouse.net_errors import ErrorKind, classify


# --- P3-37 ---------------------------------------------------------------- #
def test_write_env_rotates_changed_host_and_token(tmp_path):
    p = tmp_path / ".env"
    p.write_text("DOURMOUSE_HOST=127.0.0.1\nDOURMOUSE_ACCESS_TOKEN=old\nKEEP=x\n")
    out = ml.write_env("0.0.0.0", "new", path=p)
    assert out["changed"] is True
    text = p.read_text()
    assert "DOURMOUSE_HOST=0.0.0.0" in text
    assert "DOURMOUSE_ACCESS_TOKEN=new" in text
    assert "old" not in text
    assert "127.0.0.1" not in text
    assert "KEEP=x" in text


def test_write_env_token_only_rotation_when_host_matches(tmp_path):
    p = tmp_path / ".env"
    p.write_text("DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN=old\n")
    out = ml.write_env("0.0.0.0", "new", path=p)
    assert out["changed"] is True
    assert "DOURMOUSE_ACCESS_TOKEN=new" in p.read_text()


def test_write_env_is_private(tmp_path):
    p = tmp_path / ".env"
    ml.write_env("0.0.0.0", "tok", path=p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# --- P5-32 ---------------------------------------------------------------- #
def _http_error(code):
    return urllib.error.HTTPError("http://x/y", code, "msg", {}, io.BytesIO(b""))


def test_classify_http_error_non_listed_status_is_not_offline():
    for code in (400, 405, 409, 418, 422, 451):
        assert classify(_http_error(code)) is not ErrorKind.OFFLINE, code
        assert classify(_http_error(code)) is ErrorKind.UNKNOWN, code


def test_classify_http_408_is_timeout():
    assert classify(_http_error(408)) is ErrorKind.TIMEOUT
    assert classify("HTTP 408 from https://x") is ErrorKind.TIMEOUT


def test_classify_plain_urlerror_still_offline():
    assert classify(urllib.error.URLError("boom")) is ErrorKind.OFFLINE
    assert classify(urllib.error.URLError(ConnectionRefusedError())) is ErrorKind.OFFLINE


def test_classify_urlerror_with_unrecognised_reason_is_unknown():
    assert classify(urllib.error.URLError(object())) is ErrorKind.UNKNOWN


# --- P3-38 ---------------------------------------------------------------- #
def test_matches_tagless_name_requires_latest():
    assert model_check._matches("qwen3", ["qwen3:latest"]) is True
    assert model_check._matches("qwen3", ["qwen3:4b"]) is False
    assert model_check._matches("qwen3:4b", ["qwen3:4b"]) is True
    assert model_check._matches("qwen3", ["qwen3"]) is True


# --- H-FD2-3: the CLI says honestly whether it changed the file ------------------------------ #
def test_main_reports_already_set_when_nothing_changed(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN=tok\n")
    monkeypatch.setattr(ml, "_ENV_PATH", p)
    monkeypatch.setattr(ml, "_token_for_env", lambda rotate: "tok")
    monkeypatch.setattr(ml, "detect_addresses", lambda: {"found": False, "lan": [], "tailscale": []})
    ml.main([])
    out = capsys.readouterr().out
    assert "already set in" in out and "written to" not in out


def test_main_rotate_really_changes_the_token(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN=old\n")
    monkeypatch.setattr(ml, "_ENV_PATH", p)
    monkeypatch.setattr(ml, "detect_addresses", lambda: {"found": False, "lan": [], "tailscale": []})
    ml.main(["--rotate"])
    assert "DOURMOUSE_ACCESS_TOKEN=old" not in p.read_text()
    assert "written to" in capsys.readouterr().out
