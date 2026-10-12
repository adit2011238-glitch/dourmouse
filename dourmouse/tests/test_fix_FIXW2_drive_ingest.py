"""FIX-W2 W2R-7: Drive ingest classifies a per-file failure by HTTP status and Google's reason code,
not by message text. Permanent refusals are checkpointed as skipped; transient ones are retried."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import dourmouse.bulk_ingest as bi
from dourmouse import google_services as gs
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    yield s
    s.close()


def _http_error(code, reason=""):
    body = json.dumps({"error": {"code": code, "errors": [{"reason": reason}]}}).encode() if reason else b"{}"
    return urllib.error.HTTPError("https://x/f", code, "msg", {}, io.BytesIO(body))


def _serve(monkeypatch, files, exc):
    monkeypatch.setattr(bi, "list_all_drive_files", lambda token, page_size=1000: iter(files))

    def urlopen(request, timeout=None):
        raise exc

    monkeypatch.setattr(gs, "urlopen", urlopen)


FILE = [{"id": "f1", "name": "Big sheet", "mimeType": "application/vnd.google-apps.spreadsheet"}]


def test_http_raw_raises_a_typed_error_with_status_and_reason(monkeypatch):
    monkeypatch.setattr(gs, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_error(403, "exportSizeLimitExceeded")))
    with pytest.raises(gs.GoogleApiError) as exc:
        gs._http_raw("GET", "https://x/f", "tok")
    assert exc.value.status == 403 and exc.value.reason == "exportSizeLimitExceeded"
    assert isinstance(exc.value, RuntimeError)


@pytest.mark.parametrize("code,reason", [
    (403, "exportSizeLimitExceeded"), (403, "cannotDownloadFile"), (403, "fileNotDownloadable"), (404, "notFound"),
])
def test_permanent_refusals_are_skipped_and_checkpointed_not_errors(tmp_path, store, monkeypatch, code, reason):
    _serve(monkeypatch, FILE, _http_error(code, reason))
    ckpt = tmp_path / "c.json"
    stats = bi.ingest_drive(store, "tok", ckpt)
    assert stats["errors"] == 0 and stats["skipped_refused"] == 1
    assert "f1" in json.loads(ckpt.read_text())
    # the second run does not ask Google about it again
    calls = []
    monkeypatch.setattr(gs, "urlopen", lambda *a, **k: calls.append(1))
    again = bi.ingest_drive(store, "tok", ckpt)
    assert calls == [] and again["skipped_done"] == 1 and again["errors"] == 0


@pytest.mark.parametrize("code,reason", [
    (401, "authError"), (429, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (403, "quotaExceeded"),
    (500, ""), (503, "backendError"), (403, "insufficientPermissions"),
])
def test_transient_failures_are_errors_and_retried(tmp_path, store, monkeypatch, code, reason):
    _serve(monkeypatch, FILE, _http_error(code, reason))
    ckpt = tmp_path / "c.json"
    stats = bi.ingest_drive(store, "tok", ckpt)
    assert stats["errors"] == 1 and stats["skipped_refused"] == 0
    assert "f1" not in json.loads(ckpt.read_text())


def test_a_network_failure_is_retried(tmp_path, store, monkeypatch):
    _serve(monkeypatch, FILE, urllib.error.URLError("down"))
    stats = bi.ingest_drive(store, "tok", tmp_path / "c.json")
    assert stats["errors"] == 1


def test_over_cap_is_classified_by_the_typed_flag_not_the_message(tmp_path, store, monkeypatch):
    def reader(method, url, token, **kw):
        raise gs.GoogleApiError("something worded differently entirely", over_cap=True)

    monkeypatch.setattr(bi, "list_all_drive_files", lambda token, page_size=1000: iter(
        [{"id": "f1", "name": "big.txt", "mimeType": "text/plain"}]))
    monkeypatch.setattr("dourmouse.google_services._http_raw", reader)
    stats = bi.ingest_drive(store, "tok", tmp_path / "c.json")
    assert stats["skipped_no_text"] == 1 and stats["errors"] == 0


def test_a_real_over_cap_response_is_typed(monkeypatch):
    class Big:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * n

    monkeypatch.setattr(gs, "urlopen", lambda *a, **k: Big())
    with pytest.raises(gs.GoogleApiError) as exc:
        gs._http_raw("GET", "https://x/f", "tok", max_bytes=100)
    assert exc.value.over_cap is True


def test_http_json_errors_are_typed_too(monkeypatch):
    """Same class as _http_raw (found next to W2R-7): callers can read the status and reason."""
    monkeypatch.setattr(gs, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_error(429, "rateLimitExceeded")))
    with pytest.raises(gs.GoogleApiError) as exc:
        gs._http_json("GET", "https://x/f?y=1", "tok")
    assert exc.value.status == 429 and exc.value.reason == "rateLimitExceeded"
    assert "GOOGLE API 429 on https://x/f" in str(exc.value)
    monkeypatch.setattr(gs, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(gs.GoogleApiError) as exc2:
        gs._http_json("GET", "https://x/f", "tok")
    assert exc2.value.status is None and "request failed" in str(exc2.value)
