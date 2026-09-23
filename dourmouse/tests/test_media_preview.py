"""2026-09-23 (OS-1, the embedded media player). Before this, Dourmouse had
no audio or video playback anywhere: the only <audio>/<video> elements in any
ui/*.html were TTS output and a webcam gesture feed, and Spotify was
remote-control only (it drives the user's own separate Connect device; this
app never receives audio bytes). See docs/ENGINEERING_AUDIT.md finding #075.

These exercise the REAL route and the REAL range parser against real files on
disk, with the fake only at the socket boundary -- the house convention from
docs/TESTING.md, and the one that matters most here, because the whole point
of this feature is HTTP semantics a mocked handler would not have.
"""

from __future__ import annotations

import http.client
import threading

import pytest

from dourmouse.dispatch import DispatchRegistry, Subagent
from dourmouse.webui import (
    _MEDIA_CONTENT_TYPES,
    _PREVIEWABLE_AUDIO_EXTS,
    _PREVIEWABLE_MEDIA_EXTS,
    _PREVIEWABLE_VIDEO_EXTS,
    _parse_byte_range,
    _sandboxed_preview_path,
    run_server,
)

# Not a real encoded stream, deliberately: every assertion below is about HTTP
# byte semantics (offsets, lengths, 206/416, Content-Range), none about
# decoding. A known byte pattern makes an off-by-one in a range visible, which
# a real media file would not.
_BODY = bytes(range(256)) * 8  # 2048 bytes, every offset self-identifying


def _registry() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="echo_agent", domain="test", description="d", tools=()))
    return reg


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    out = (resp.status, body, dict(resp.getheaders()))
    conn.close()
    return out


@pytest.fixture
def media_file(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(_BODY)
    return target


def _media_url(target) -> str:
    import urllib.parse

    return "/api/files/media?path=" + urllib.parse.quote(str(target))


class TestParseByteRange:
    """The range parser in isolation. Its three return shapes are load
    bearing: a tuple is a real 206, None means 'send the whole thing, 200',
    and the literal string 'unsatisfiable' means a real 416."""

    def test_no_header_means_whole_body(self):
        assert _parse_byte_range(None, 2048) is None
        assert _parse_byte_range("", 2048) is None

    def test_closed_range(self):
        assert _parse_byte_range("bytes=0-99", 2048) == (0, 99)
        assert _parse_byte_range("bytes=100-199", 2048) == (100, 199)

    def test_open_ended_range_runs_to_the_real_end(self):
        assert _parse_byte_range("bytes=2000-", 2048) == (2000, 2047)

    def test_suffix_range_is_the_last_n_bytes(self):
        assert _parse_byte_range("bytes=-500", 2048) == (1548, 2047)

    def test_suffix_larger_than_the_file_clamps_to_the_whole_file(self):
        assert _parse_byte_range("bytes=-9999", 2048) == (0, 2047)

    def test_end_past_the_file_is_clamped_not_rejected(self):
        # A media element routinely asks for more than exists at the tail.
        assert _parse_byte_range("bytes=2000-99999", 2048) == (2000, 2047)

    def test_start_past_the_file_is_unsatisfiable(self):
        assert _parse_byte_range("bytes=5000-6000", 2048) == "unsatisfiable"

    def test_open_ended_start_past_the_file_is_also_unsatisfiable(self):
        # Regression, caught live rather than by the closed-range case above.
        # With no end given, `end` becomes size-1, which is LESS than start --
        # so an inverted-range check placed before the past-the-end check
        # wrongly classified this as malformed and served a full 200.
        assert _parse_byte_range("bytes=99999999-", 2048) == "unsatisfiable"

    def test_zero_suffix_is_unsatisfiable(self):
        # "bytes=-0" asks for the last zero bytes, which RFC 9110 calls
        # unsatisfiable rather than an empty success.
        assert _parse_byte_range("bytes=-0", 2048) == "unsatisfiable"

    def test_empty_file_never_produces_a_range(self):
        assert _parse_byte_range("bytes=0-10", 0) is None

    def test_unsupported_syntax_falls_back_to_whole_body(self):
        for header in ("items=0-10", "bytes=abc-def", "bytes=10", "bytes=50-10", "bytes=-"):
            assert _parse_byte_range(header, 2048) is None, header

    def test_multi_range_is_declined_not_half_answered(self):
        # Serving only the first of several requested ranges would be a wrong
        # answer dressed as a right one. Falling back to a full 200 is legal.
        assert _parse_byte_range("bytes=0-99,200-299", 2048) is None


class TestMediaRouteServesRealBytes:
    def test_full_request_returns_whole_file_and_advertises_ranges(self, server, media_file):
        _, port = server
        status, body, headers = _get(port, _media_url(media_file))
        assert status == 200
        assert body == _BODY
        # Without this header a <video> element will not attempt to seek.
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Length"] == str(len(_BODY))
        assert headers["Content-Type"] == "video/mp4"

    def test_range_request_returns_206_with_exactly_those_bytes(self, server, media_file):
        _, port = server
        status, body, headers = _get(
            port, _media_url(media_file), {"Range": "bytes=100-199"}
        )
        assert status == 206
        assert body == _BODY[100:200]
        assert headers["Content-Range"] == f"bytes 100-199/{len(_BODY)}"
        assert headers["Content-Length"] == "100"

    def test_open_ended_range_reaches_the_real_end(self, server, media_file):
        _, port = server
        status, body, headers = _get(port, _media_url(media_file), {"Range": "bytes=2000-"})
        assert status == 206
        assert body == _BODY[2000:]
        assert headers["Content-Range"] == f"bytes 2000-2047/{len(_BODY)}"

    def test_suffix_range_returns_the_tail(self, server, media_file):
        _, port = server
        status, body, _ = _get(port, _media_url(media_file), {"Range": "bytes=-64"})
        assert status == 206
        assert body == _BODY[-64:]

    def test_range_past_the_end_is_a_real_416_carrying_the_real_size(self, server, media_file):
        _, port = server
        status, _, headers = _get(port, _media_url(media_file), {"Range": "bytes=9000-9999"})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{len(_BODY)}"

    @pytest.mark.parametrize("header", ["bytes=9000-9999", "bytes=9000-"])
    def test_both_shapes_of_a_past_the_end_seek_are_416(self, server, media_file, header):
        # A media element seeking near the end of a file it has stale metadata
        # for sends the open-ended shape, which is exactly the one that was
        # broken. Both must answer 416, never a silent full-file 200.
        _, port = server
        status, _, headers = _get(port, _media_url(media_file), {"Range": header})
        assert status == 416, header
        assert headers["Content-Range"] == f"bytes */{len(_BODY)}"

    def test_a_larger_than_one_chunk_file_streams_whole(self, server, tmp_path):
        # _MEDIA_CHUNK is 256KB and the body is written in a loop, so a file
        # spanning several chunks is the case where a loop bug would show.
        big = tmp_path / "long.mp4"
        payload = bytes(range(256)) * 4096  # 1 MiB, 4 chunks
        big.write_bytes(payload)
        _, port = server
        status, body, _ = _get(port, _media_url(big))
        assert status == 200
        assert body == payload

    def test_audio_gets_its_own_real_content_type(self, server, tmp_path):
        track = tmp_path / "song.mp3"
        track.write_bytes(_BODY)
        _, port = server
        status, _, headers = _get(port, _media_url(track))
        assert status == 200
        assert headers["Content-Type"] == "audio/mpeg"

    def test_cors_is_present_because_the_pane_iframe_has_an_opaque_origin(
        self, server, media_file
    ):
        # Same real reason every other /api/files/* route opts in: the pane's
        # sandbox drops allow-same-origin, so this is cross-origin to the
        # browser even though it is this exact server.
        _, port = server
        _, _, headers = _get(port, _media_url(media_file))
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "Content-Range" in headers["Access-Control-Expose-Headers"]


class TestMediaRouteRefusesHonestly:
    def test_missing_path_is_refused(self, server):
        _, port = server
        status, body, _ = _get(port, "/api/files/media")
        assert status == 400
        assert b"bad or missing media path" in body

    def test_a_nonexistent_file_is_refused_not_streamed_as_empty(self, server, tmp_path):
        _, port = server
        status, _, _ = _get(port, _media_url(tmp_path / "nope.mp4"))
        assert status == 400

    def test_a_non_media_extension_is_refused_on_this_route(self, server, tmp_path):
        # A PDF is genuinely previewable, just not through THIS route -- the
        # route must not become a general file-read primitive.
        doc = tmp_path / "paper.pdf"
        doc.write_bytes(b"%PDF-1.4\n")
        _, port = server
        status, _, _ = _get(port, _media_url(doc))
        assert status == 400

    def test_an_undecodable_container_is_refused_rather_than_half_served(
        self, server, tmp_path
    ):
        # .mkv is a real media file a browser cannot natively decode. Serving
        # it would produce a silently blank player, which is the fabrication
        # this codebase refuses everywhere else.
        mkv = tmp_path / "movie.mkv"
        mkv.write_bytes(_BODY)
        _, port = server
        status, _, _ = _get(port, _media_url(mkv))
        assert status == 400


class TestMediaExtensionsAreInternallyConsistent:
    def test_every_media_extension_has_a_real_content_type(self):
        # A missing entry would KeyError at request time on a path the
        # sandbox had already accepted.
        for ext in _PREVIEWABLE_MEDIA_EXTS:
            assert ext in _MEDIA_CONTENT_TYPES, ext

    def test_audio_and_video_sets_do_not_overlap(self):
        assert not (_PREVIEWABLE_AUDIO_EXTS & _PREVIEWABLE_VIDEO_EXTS)

    def test_content_types_match_their_kind(self):
        for ext in _PREVIEWABLE_AUDIO_EXTS:
            assert _MEDIA_CONTENT_TYPES[ext].startswith("audio/"), ext
        for ext in _PREVIEWABLE_VIDEO_EXTS:
            assert _MEDIA_CONTENT_TYPES[ext].startswith("video/"), ext

    def test_the_sandbox_now_accepts_media_paths(self, tmp_path):
        clip = tmp_path / "clip.webm"
        clip.write_bytes(_BODY)
        assert _sandboxed_preview_path(str(clip)) == clip.resolve()

    def test_the_tool_and_the_server_agree_on_what_is_playable(self):
        # Two lists that can drift is a real bug class: the tool would open a
        # pane for a file the server then refuses.
        from dourmouse import system_access

        assert system_access._PREVIEWABLE_AUDIO_EXTS == _PREVIEWABLE_AUDIO_EXTS
        assert system_access._PREVIEWABLE_VIDEO_EXTS == _PREVIEWABLE_VIDEO_EXTS


class TestOpenFilePreviewToolHandlesMedia:
    def test_it_refuses_an_undecodable_container_and_names_the_alternative(self, tmp_path):
        from dourmouse.system_access import _open_file_preview_tool

        mkv = tmp_path / "movie.mkv"
        mkv.write_bytes(b"x")
        out = _open_file_preview_tool({"path": str(mkv)})
        assert out.startswith("REFUSED:")
        assert "open_path" in out

    def test_it_reports_a_media_open_distinctly_from_a_document_open(
        self, tmp_path, monkeypatch
    ):
        import dourmouse.system_access as sa

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        # Fake ONLY the socket call to the running server, so the real
        # extension routing and the real message construction still run.
        opened: list[str] = []

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            opened.append(req.full_url)
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        out = sa._open_file_preview_tool({"path": str(clip)})
        assert out.startswith("OPENED MEDIA PLAYER:")
        assert "video" in out
        # It must not claim to have started playback, which it cannot do.
        assert "does not press play" in out
        assert opened and "/api/browser-pane/open" in opened[0]


class TestPreviewPaneUrlIsSameOrigin:
    """2026-09-23 (OS-1). Found during live verification: the pane was being
    handed an absolute http://127.0.0.1:<port>/... URL for this app's OWN
    preview page, while the console itself may be loaded as localhost. To a
    browser those are different origins, so framing our own page was a real
    cross-origin embed -- which is why the pane's own code comments say
    back/forward 'isn't reachable from the parent page'. A root-relative URL
    resolves against the console's origin and makes it same-origin."""

    def test_the_tool_hands_the_pane_a_relative_url(self, tmp_path, monkeypatch):
        import dourmouse.system_access as sa

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        sent: list[dict] = []

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            import json as _j

            sent.append({"url": req.full_url, "body": _j.loads(req.data.decode())})
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        sa._open_file_preview_tool({"path": str(clip)})

        assert len(sent) == 1
        # The API call itself still has to be absolute: this tool can run in a
        # separate subprocess with no page context, which is the whole reason
        # it posts over HTTP instead of touching the in-process singleton.
        assert sent[0]["url"].startswith("http://127.0.0.1:")
        # What goes INTO the pane is relative.
        pane_url = sent[0]["body"]["url"]
        assert pane_url.startswith("/file_preview.html?")
        assert not pane_url.startswith("//")
        assert "127.0.0.1" not in pane_url

    def test_the_open_endpoint_accepts_a_relative_url(self, server):
        import json as _j

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/browser-pane/open",
            body=_j.dumps({"url": "/file_preview.html?src=files&path=/tmp/x.mp4"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = _j.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert payload == {"ok": True}

    def test_the_open_endpoint_still_accepts_a_real_absolute_url(self, server):
        import json as _j

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/browser-pane/open",
            body=_j.dumps({"url": "https://example.com/page"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        conn.read = resp.read()
        conn.close()
        assert resp.status == 200

    def test_a_protocol_relative_url_is_refused_not_treated_as_same_origin(self, server):
        # "//evil.example/x" looks relative and is NOT. Accepting it as
        # same-origin would frame a foreign site with this app's own trust.
        import json as _j

        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/browser-pane/open",
            body=_j.dumps({"url": "//evil.example/x"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = _j.loads(resp.read())
        conn.close()
        assert resp.status == 400
        assert payload["ok"] is False

    def test_a_non_http_scheme_is_still_refused(self, server):
        import json as _j

        _, port = server
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST",
                "/api/browser-pane/open",
                body=_j.dumps({"url": bad}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            conn.close()
            assert resp.status == 400, bad


class TestMediaRouteIsNotAFileReadPrimitive:
    """A new route that streams arbitrary absolute paths is a real security
    surface, so these pin the actual behaviour rather than assuming the
    existing sandbox covers it. All verified live against the running server
    as well (see EVIDENCE/002_media_player.txt)."""

    @pytest.mark.parametrize(
        "raw",
        [
            "../../../../etc/passwd",
            "/etc/passwd",
            "/etc/hosts",
            "",
            "/dev/zero",
        ],
    )
    def test_non_media_and_traversal_paths_are_refused(self, server, raw):
        import urllib.parse

        _, port = server
        status, _, _ = _get(port, "/api/files/media?path=" + urllib.parse.quote(raw))
        assert status == 400, raw

    def test_a_traversal_suffix_on_a_real_media_path_is_refused(self, server, media_file):
        # ".../clip.mp4/../../../etc/passwd" resolves away from the media file
        # entirely; the extension check must run on the RESOLVED target.
        import urllib.parse

        _, port = server
        raw = str(media_file) + "/../../../../../../etc/passwd"
        status, _, _ = _get(port, "/api/files/media?path=" + urllib.parse.quote(raw))
        assert status == 400

    def test_a_symlink_named_like_media_cannot_smuggle_a_non_media_file(
        self, server, tmp_path, media_file
    ):
        # The load-bearing ordering: _sandboxed_preview_path resolves FIRST and
        # checks the extension of what it actually resolved to. A check on the
        # given NAME instead would serve /etc/hosts through a ".mp4" symlink.
        import urllib.parse

        secret = tmp_path / "secret.conf"
        secret.write_text("not media")
        link = tmp_path / "looks_like.mp4"
        try:
            link.symlink_to(secret)
        except OSError:  # pragma: no cover - symlinks unavailable
            pytest.skip("symlinks not supported here")
        _, port = server
        status, _, _ = _get(port, "/api/files/media?path=" + urllib.parse.quote(str(link)))
        assert status == 400

    def test_a_symlink_to_real_media_is_served(self, server, tmp_path, media_file):
        # The other half of the same rule: resolution decides, so a symlink
        # whose TARGET is real media is legitimately served. Without this the
        # test above would also pass on a blanket "refuse all symlinks", which
        # is not what the code does and not what it should do.
        import urllib.parse

        link = tmp_path / "alias.mp4"
        try:
            link.symlink_to(media_file)
        except OSError:  # pragma: no cover
            pytest.skip("symlinks not supported here")
        _, port = server
        status, body, _ = _get(port, "/api/files/media?path=" + urllib.parse.quote(str(link)))
        assert status == 200
        assert body == _BODY
