"""FI1 fixes for dourmouse/google_services.py: P3-30 gmail_send validation,
P3-34 bounded drive_download read, P3-29 IMAP query sanitising, P3-31 Drive q
escaping, P3-32 id validation, P3-33 partial-create reporting. Everything runs
against stubbed REST helpers; no network, no real account."""

from __future__ import annotations

import urllib.parse

import pytest

from dourmouse import google_services as gs


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    calls: list[tuple] = []
    return calls


# --------------------------------------------------------------------------- #
# P3-30: gmail_send validates before the OAuth send
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("to,subject,why", [
    ("", "hi", "recipient"),
    ("nobody", "hi", "recipient"),
    ("a@b.com", "", "subject"),
    ("a@b.com", "   ", "subject"),
    ("a@b.com\r\nBcc: evil@x.com", "hi", "recipient"),
    ("a@b.com", "hi\nBcc: evil@x.com", "subject"),
])
def test_oauth_send_refuses_bad_input_before_any_request(monkeypatch, oauth, to, subject, why):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: oauth.append(a) or {"id": "x"})
    out = gs.gmail_send(to, subject, "body")
    assert out.startswith("ERROR"), out
    assert why in out
    assert oauth == []


def test_oauth_send_still_sends_a_valid_message(monkeypatch, oauth):
    monkeypatch.setattr(gs, "_http_json", lambda m, u, t, b=None: oauth.append((m, u, b)) or {"id": "m1"})
    out = gs.gmail_send("a@b.com", "Hello", "body")
    assert out.startswith("GMAIL SEND OK")
    assert len(oauth) == 1


def test_oauth_send_value_error_from_header_is_an_error_string(monkeypatch, oauth):
    # Defence in depth: even if a header value slipped past validation, a
    # ValueError from EmailMessage must come back as the ERROR string.
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: {"id": "x"})
    out = gs._gmail_send_oauth("tok", "a@b.com\nX: y", "s", "b")
    assert out.startswith("ERROR")


# --------------------------------------------------------------------------- #
# P3-34: drive_download bounds the read
# --------------------------------------------------------------------------- #

class _FakeResp:
    status = 200

    def __init__(self, total, ctype="application/octet-stream", chunk=65536):
        self.headers = {"Content-Type": ctype}
        self.total = total
        self.sent = 0
        self.chunk = chunk
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        self.read_calls += 1
        if n is None or n < 0:
            n = self.total - self.sent
        n = min(n, self.total - self.sent)
        self.sent += n
        return b"x" * n


def test_http_get_stops_reading_past_the_cap(monkeypatch):
    resp = _FakeResp(10 * 1024 * 1024)
    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *a, **k: resp)
    with pytest.raises(gs.BodyTooLarge):
        gs._http_get("http://example.com/x", max_bytes=1024 * 1024)
    assert resp.sent <= 1024 * 1024 + 65536


def test_drive_download_refuses_oversize_without_reading_it_all(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    resp = _FakeResp(80 * 1024 * 1024)
    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *a, **k: resp)
    out = gs.drive_download("abc123")
    assert "50 MB" in out and "REFUSED" in out
    assert resp.sent < 60 * 1024 * 1024


def test_drive_download_small_file_still_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    resp = _FakeResp(2048)
    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *a, **k: resp)
    out = gs.drive_download("abc123")
    assert out.startswith("DRIVE DOWNLOAD OK"), out
    assert "2048 bytes" in out


# --------------------------------------------------------------------------- #
# P3-29: IMAP query cannot carry control characters or a trailing backslash
# --------------------------------------------------------------------------- #

class _FakeImap:
    def __init__(self):
        self.searches: list[str] = []

    def select(self, *a, **k):
        return "OK", [b""]

    def search(self, charset, criteria):
        self.searches.append(criteria)
        if criteria == "ALL":
            return "OK", [b"1 2 3"]
        return "OK", [b"2"]

    def fetch(self, mid, spec):
        return "OK", [(b"1", b"From: a@b.com\r\nSubject: s\r\nDate: d\r\n\r\n")]

    def logout(self):
        pass


@pytest.mark.parametrize("query", [
    'from:x"\r\nA2 SELECT INBOX\r\nA3 STORE 1:* +FLAGS (\\Deleted)\r\nA4 EXPUNGE',
    "from:x\\",
    "plain words\nwith newline",
    "nul\x00byte",
])
def test_imap_search_never_sends_control_chars_or_unescaped_backslash(monkeypatch, query):
    monkeypatch.setattr(gs, "_oauth_access_token", lambda: None)
    monkeypatch.setattr(gs, "_oauth_user_needs_reauth", lambda action: None)
    conn = _FakeImap()
    monkeypatch.setattr(gs, "_imap", lambda: conn)
    gs.gmail_search(query)
    sent = [s for s in conn.searches if s != "ALL"]
    assert sent
    for crit in sent:
        assert "\r" not in crit and "\n" not in crit and "\x00" not in crit, repr(crit)
        # every backslash is doubled, so none can escape the closing quote
        body = crit.replace("\\\\", "")
        assert "\\" not in body, repr(crit)


# --------------------------------------------------------------------------- #
# P3-31: Drive q escaping
# --------------------------------------------------------------------------- #

def _drive_q(monkeypatch, query, file_type=None):
    seen = {}

    def fake(method, url, token, body=None):
        seen["url"] = url
        return {"files": []}

    monkeypatch.setattr(gs, "_http_json", fake)
    gs._drive_search_oauth("tok", query, 5, file_type)
    return urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)["q"][0]


def test_apostrophe_is_backslash_escaped(monkeypatch):
    q = _drive_q(monkeypatch, "Dave's notes")
    assert "name contains 'Dave\\'s notes'" in q
    assert "''" not in q


def test_backslash_is_escaped_first(monkeypatch):
    q = _drive_q(monkeypatch, "a\\b")
    assert "'a\\\\b'" in q


def test_injection_attempt_stays_inside_the_literal(monkeypatch):
    q = _drive_q(monkeypatch, "x' or trashed = true or name contains '")
    assert "\\' or trashed" in q


def test_file_type_literal_is_escaped(monkeypatch):
    q = _drive_q(monkeypatch, "", file_type="a'b")
    assert "mimeType = 'a\\'b'" in q


# --------------------------------------------------------------------------- #
# P3-32: ids are validated before they reach a URL
# --------------------------------------------------------------------------- #

BAD_IDS = ["X/permissions/..", "a?b=c", "a#b", "../x", "a b", "a%2Fb"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_drive_share_rejects_path_like_ids(monkeypatch, oauth, bad):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: oauth.append(a) or {})
    out = gs.drive_share(bad, "a@b.com")
    assert "ERROR" in out and "Nothing was shared" in out
    assert oauth == []


@pytest.mark.parametrize("bad", BAD_IDS)
def test_drive_read_rejects_path_like_ids(monkeypatch, oauth, bad):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: oauth.append(a) or {})
    monkeypatch.setattr(gs, "_http_raw", lambda *a, **k: oauth.append(a) or b"")
    out = gs.drive_read(bad)
    assert out.startswith("ERROR")
    assert oauth == []


@pytest.mark.parametrize("bad", BAD_IDS)
def test_docs_append_and_image_reject_path_like_ids(monkeypatch, oauth, bad):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: oauth.append(a) or {})
    assert gs.docs_append(bad, "text").startswith("ERROR")
    assert gs.docs_insert_image(bad, "https://example.com/i.png").startswith("ERROR")
    assert oauth == []


@pytest.mark.parametrize("bad", BAD_IDS)
def test_gmail_read_oauth_rejects_path_like_ids(monkeypatch, oauth, bad):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: oauth.append(a) or {})
    out = gs.gmail_read(bad)
    assert out.startswith("ERROR")
    assert oauth == []


def test_valid_ids_still_work(monkeypatch, oauth):
    seen = []
    monkeypatch.setattr(gs, "_http_json", lambda m, u, t, b=None: seen.append(u) or {"payload": {}})
    gs.gmail_read("18c3f0a1b2c3d4e5")
    assert seen and seen[0].startswith(f"{gs._GMAIL_API}/messages/18c3f0a1b2c3d4e5?")


# --------------------------------------------------------------------------- #
# P3-33: a half-finished create names what exists
# --------------------------------------------------------------------------- #

def test_drive_doc_content_failure_names_the_created_doc(monkeypatch, oauth):
    monkeypatch.setattr(gs, "_http_json", lambda *a, **k: {"id": "DOC123abc"})

    def fail(*a, **k):
        raise gs.GoogleApiError("GOOGLE API 500 on upload: boom", status=500)

    monkeypatch.setattr(gs, "_http_raw", fail)
    out = gs.drive_create_doc("My Doc", "some content")
    assert "DOC123abc" in out
    assert "was created" in out and "not written" in out
    assert "500" in out


def test_slides_first_batch_failure_names_the_created_deck(monkeypatch, oauth):
    def fake(method, url, token, body=None):
        if url == gs._SLIDES_API:
            return {"presentationId": "DECK999", "slides": [{"objectId": "p1"}]}
        raise gs.GoogleApiError("GOOGLE API 429 on batchUpdate: slow down", status=429)

    monkeypatch.setattr(gs, "_http_json", fake)
    out = gs.slides_create("Deck", [{"title": "t", "body": "b"}])
    assert "DECK999" in out and "was created" in out and "429" in out


def test_slides_text_batch_failure_names_the_created_deck(monkeypatch, oauth):
    state = {"n": 0}

    def fake(method, url, token, body=None):
        if url == gs._SLIDES_API:
            return {"presentationId": "DECK999", "slides": [{"objectId": "p1"}]}
        state["n"] += 1
        if state["n"] == 1:
            return {}
        raise gs.GoogleApiError("GOOGLE API 503 on batchUpdate: down", status=503)

    monkeypatch.setattr(gs, "_http_json", fake)
    out = gs.slides_create("Deck", [{"title": "t", "body": "b"}])
    assert "DECK999" in out and "was created" in out and "503" in out
