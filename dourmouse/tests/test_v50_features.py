"""v5.0 feature tests — upload, setup panel, Codex backend, Gmail, fast dispatch.

Covers the new user-facing capabilities: the sandboxed /api/upload flow, the
/api/setup capability checklist, the Codex coding backend resolution, the
Gmail (IMAP/SMTP) module's honest NOT CONFIGURED contract, the fast-dispatch
orchestrator model default, and the read_upload tool's sandbox.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

import pytest

from dourmouse.tests.test_webui import _echo_registry


@pytest.fixture
def server(monkeypatch, tmp_path):
    from dourmouse.webui import run_server

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def _post(port, path, body: bytes, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# --------------------------------------------------------------------------- #
# A2 — file upload
# --------------------------------------------------------------------------- #

class TestUpload:
    def test_upload_round_trip(self, server):
        _, port = server
        status, body = _post(
            port,
            "/api/upload?name=notes.txt",
            b"hello world upload",
            {"Content-Type": "application/octet-stream"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True and data["name"] == "notes.txt"
        assert data["size"] == len(b"hello world upload")
        # listed
        status, body = _get(port, "/api/files")
        assert status == 200
        files = json.loads(body)["files"]
        assert any(f["name"] == "notes.txt" for f in files)
        # served back
        status, body = _get(port, "/uploads/notes.txt")
        assert status == 200 and body == "hello world upload"

    def test_api_files_skips_dotfiles(self, server, monkeypatch, tmp_path):
        # v13.5 (live-caught, real bug): this project's own workspace/
        # uploads/ lives on an ExFAT external volume, which makes macOS
        # synthesize a real "._<name>" AppleDouble sidecar next to every
        # real file written there -- confirmed live, uploading a real PDF
        # produced both edge_report.pdf AND ._edge_report.pdf, and this
        # listing showed both as if the user had uploaded two files.
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        uploads = tmp_path / "ws" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "real.txt").write_text("real content")
        (uploads / "._real.txt").write_bytes(b"\x00\x05Mac OS X junk")  # a real AppleDouble-shaped sidecar
        (uploads / ".DS_Store").write_bytes(b"junk")
        _, port = server
        status, body = _get(port, "/api/files")
        assert status == 200
        names = [f["name"] for f in json.loads(body)["files"]]
        assert names == ["real.txt"]

    def test_upload_rejects_path_escape(self, server):
        _, port = server
        status, body = _post(
            port, "/api/upload?name=..%2F..%2Fevil.txt", b"x"
        )
        assert status == 400
        assert b"filename must be" in body or b"bad" in body

    def test_upload_rejects_bad_name(self, server):
        _, port = server
        status, _body = _post(port, "/api/upload?name=a%2Fb.txt", b"x")
        assert status == 400

    def test_upload_requires_body(self, server):
        _, port = server
        status, body = _post(port, "/api/upload?name=empty.txt", b"")
        assert status == 400
        assert b"empty" in body

    def test_upload_path_traversal_in_get_404(self, server):
        _, port = server
        status, _body = _get(port, "/uploads/..%2F..%2Fetc%2Fpasswd")
        assert status in (400, 404)


# --------------------------------------------------------------------------- #
# v13.4 — upload into the shared RAG database
# --------------------------------------------------------------------------- #

class TestRagUpload:
    """POST /api/rag/upload — real user request: "a page where files can
    be uploaded to the shared rag database". Same sandboxed save contract
    as /api/upload above, plus real indexing into the same MemoryStore
    query_shared_memory searches (dourmouse/memory_store.py)."""

    def test_text_file_saved_and_indexed(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        _, port = server
        status, body = _post(
            port, "/api/rag/upload?name=notes.txt",
            b"the quick brown fox jumps over the lazy dog",
        )
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        assert data["indexed"] is True
        assert data["indexed_chars"] == len(b"the quick brown fox jumps over the lazy dog")

        from dourmouse.memory_store import MemoryStore

        store = MemoryStore(tmp_path / "ws" / "memory" / "atlas_memory.db")
        hits = store.search("fox", limit=5)
        assert any("notes.txt" in h["title"] for h in hits)
        store.close()

    def test_file_saved_even_when_nothing_extractable(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        _, port = server
        status, body = _post(port, "/api/rag/upload?name=photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        assert data["indexed"] is False
        assert "reason" in data
        # the raw bytes were still genuinely saved, same as /api/upload
        # (binary content -- read raw, not through _get's UTF-8 decode).
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/uploads/photo.png")
        resp = conn.getresponse()
        assert resp.status == 200
        conn.close()

    def test_rejects_bad_name_same_as_plain_upload(self, server):
        _, port = server
        status, _body = _post(port, "/api/rag/upload?name=a%2Fb.txt", b"x")
        assert status == 400

    def test_rejects_empty_body(self, server):
        _, port = server
        status, body = _post(port, "/api/rag/upload?name=empty.txt", b"")
        assert status == 400
        assert b"empty" in body

    def test_pdf_extraction_reuses_bulk_ingest_extractor(self, server, monkeypatch, tmp_path):
        """Not a real PDF byte stream here (that's covered by
        dourmouse/tests/test_extract.py's own real pypdf tests) — this
        confirms the endpoint calls bulk_ingest._extract_local_text at
        all, i.e. one extractor shared with the bulk scans, not a second
        implementation living only in webui.py."""
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        calls = []

        def fake_extract(path):
            calls.append(str(path))
            return "extracted pdf text"

        monkeypatch.setattr("dourmouse.bulk_ingest._extract_local_text", fake_extract)
        _, port = server
        status, body = _post(port, "/api/rag/upload?name=doc.pdf", b"%PDF-fake-bytes")
        assert status == 200
        assert json.loads(body)["indexed"] is True
        assert len(calls) == 1 and calls[0].endswith("doc.pdf")


def _write_minimal_pdf(path, text="Hello PDF"):
    """Same real, hand-built minimal-valid-PDF fixture as
    test_pdf_reader.py's own — duplicated here (not imported cross-file)
    so this HTTP-endpoint test file stays independently runnable, same
    convention as the rest of this codebase's test suite."""
    content = f"BT /F1 18 Tf 10 50 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
        b"/MediaBox [0 0 200 100] /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    path.write_bytes(pdf)


class TestPdfEndpoints:
    """GET /api/pdf/info, /api/pdf/text, /api/pdf/page.png — v13.5, Vision
    OS "GPU-accelerated technical document reader" (dourmouse/pdf_reader.py
    — real pypdfium2, see that module's own docstring for what's real vs.
    Marker, explicitly not built). Sandboxed to the uploads root, same
    whitelist+resolve+relative_to pattern the pre-existing /uploads/
    handler already uses (dourmouse/webui.py's _sandboxed_upload_path)."""

    def _write_pdf(self, tmp_path, name="doc.pdf", text="Hello PDF"):
        uploads = tmp_path / "ws" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        p = uploads / name
        _write_minimal_pdf(p, text)
        return p

    def test_info_reports_the_real_page_count(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        self._write_pdf(tmp_path)
        _, port = server
        status, body = _get(port, "/api/pdf/info?name=doc.pdf")
        assert status == 200
        assert json.loads(body) == {"ok": True, "page_count": 1}

    def test_text_returns_the_real_extracted_text(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        self._write_pdf(tmp_path)
        _, port = server
        status, body = _get(port, "/api/pdf/text?name=doc.pdf&page=0")
        assert status == 200
        assert json.loads(body)["text"] == "Hello PDF"

    def test_page_png_returns_a_real_image(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        self._write_pdf(tmp_path)
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/pdf/page.png?name=doc.pdf&page=0")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/png"
        assert body.startswith(b"\x89PNG\r\n\x1a\n")

    def test_missing_file_is_honest_not_a_crash(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        _, port = server
        status, body = _get(port, "/api/pdf/info?name=nope.pdf")
        assert status == 400  # fails the sandbox's is_file() check -> bad name path
        assert json.loads(body)["ok"] is False

    def test_path_traversal_is_refused(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        _, port = server
        status, _body = _get(port, "/api/pdf/info?name=" + urllib.parse.quote("../../etc/passwd"))
        assert status == 400

    def test_out_of_range_page_png_is_a_404_not_a_crash(self, server, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        self._write_pdf(tmp_path)
        _, port = server
        status, _body = _get(port, "/api/pdf/page.png?name=doc.pdf&page=99")
        assert status == 404


# --------------------------------------------------------------------------- #
# A7 — setup status panel
# --------------------------------------------------------------------------- #

class TestSetupPanel:
    def test_setup_returns_capability_checklist(self, server):
        _, port = server
        status, body = _get(port, "/api/setup")
        assert status == 200
        items = json.loads(body)["items"]
        for key in (
            "llm_backend",
            "voice",
            "codex",
            "deepseek",
            "claude",
            "gmail",
            "upload",
            "memory",
            "live",
        ):
            assert key in items, f"setup item missing: {key}"
            assert "configured" in items[key]
            assert "hint" in items[key]


class TestSetupStatusMemoryCountBoundedWait:
    """v13.5 (live-diagnosed, real bug): mem.count() is a real, uncached
    network call for a RemoteMemoryStore, up to its own real 15s timeout
    — measured live at 15.6s on the first /api/setup poll after a fresh
    server start. Bounded here to a real 2s wait with an honest
    "checking…" fallback rather than blocking the whole request."""

    def test_a_slow_memory_store_does_not_block_past_the_bound(self):
        import time
        import types

        from dourmouse import webui as webui_module

        class _SlowMemory:
            def count(self):
                time.sleep(5.0)  # real sleep, well past the 2s bound
                return 999

        server = types.SimpleNamespace(config=None, memory=_SlowMemory(), live_runtime=None)
        started = time.monotonic()
        result = webui_module._build_setup_status_uncached(server)
        elapsed = time.monotonic() - started
        assert elapsed < 4.0, f"blocked for {elapsed}s, past the real 2s bound"
        assert "checking" in result["items"]["memory"]["detail"].lower()
        assert result["items"]["memory"]["configured"] is True

    def test_a_fast_memory_store_reports_the_real_count(self):
        import types

        from dourmouse import webui as webui_module

        class _FastMemory:
            def count(self):
                return 42

        server = types.SimpleNamespace(config=None, memory=_FastMemory(), live_runtime=None)
        result = webui_module._build_setup_status_uncached(server)
        assert result["items"]["memory"]["detail"] == "42 facts"

    def test_a_raising_memory_store_is_still_honest_not_a_crash(self):
        import types

        from dourmouse import webui as webui_module

        class _BrokenMemory:
            def count(self):
                raise RuntimeError("store corrupted")

        server = types.SimpleNamespace(config=None, memory=_BrokenMemory(), live_runtime=None)
        result = webui_module._build_setup_status_uncached(server)
        # A fast raise still lands as "no real number yet" via the same
        # bounded-wait path (the thread finishes almost instantly with an
        # error, not a count) -- never a crash, never a fabricated number.
        assert result["items"]["memory"]["configured"] is True
        assert "checking" in result["items"]["memory"]["detail"].lower()


class TestSetupStatusCache:
    """v13.5 (live-diagnosed, real bug — "it didn't load properly in
    preview"): a real GET /api/setup on the actual machine measured
    15.4s (compute-node health probe called TWICE + a slow `claude
    --version` subprocess + a Keychain lookup, none of it cached at the
    whole-function level). See webui.build_setup_status's own docstring
    for the full diagnosis. These test the cache wrapper directly
    (webui._build_setup_status_uncached monkeypatched to a counting spy)
    rather than going through a real server + real probes — fast,
    deterministic, and isolates the caching behavior itself from
    whatever the real probes happen to return on this machine."""

    def _fake_server(self):
        import types

        return types.SimpleNamespace(config=None, memory=None, live_runtime=None)

    def test_second_call_within_ttl_is_served_from_cache(self, monkeypatch):
        from dourmouse import webui as webui_module

        calls = []
        monkeypatch.setattr(
            webui_module, "_build_setup_status_uncached",
            lambda server: calls.append(1) or {"items": {"marker": "real"}},
        )
        server = self._fake_server()
        first = webui_module.build_setup_status(server)
        second = webui_module.build_setup_status(server)
        assert first == second == {"items": {"marker": "real"}}
        assert len(calls) == 1  # the SECOND call must be served from cache

    def test_different_server_objects_never_share_a_cached_result(self, monkeypatch):
        # The real safety property that makes this cache correct for
        # tests (and for anything that ever constructs a second server):
        # id(server)-keyed, not a bare time-only cache.
        from dourmouse import webui as webui_module

        calls = []

        def fake(server):
            calls.append(server)
            return {"items": {"n": len(calls)}}

        monkeypatch.setattr(webui_module, "_build_setup_status_uncached", fake)
        server_a = self._fake_server()
        server_b = self._fake_server()
        result_a = webui_module.build_setup_status(server_a)
        result_b = webui_module.build_setup_status(server_b)
        assert result_a == {"items": {"n": 1}}
        assert result_b == {"items": {"n": 2}}
        assert len(calls) == 2  # both real, neither served the other's cache

    def test_cache_expires_after_the_ttl(self, monkeypatch):
        from dourmouse import webui as webui_module

        calls = []
        monkeypatch.setattr(
            webui_module, "_build_setup_status_uncached",
            lambda server: calls.append(1) or {"items": {}},
        )
        monkeypatch.setenv("DOURMOUSE_SETUP_CACHE_TTL", "0")  # expires immediately
        server = self._fake_server()
        webui_module.build_setup_status(server)
        webui_module.build_setup_status(server)
        assert len(calls) == 2  # TTL=0 -> never served from cache

    def test_default_ttl_is_a_real_positive_number(self, monkeypatch):
        from dourmouse import webui as webui_module

        monkeypatch.delenv("DOURMOUSE_SETUP_CACHE_TTL", raising=False)
        assert webui_module._setup_status_cache_ttl() == webui_module._SETUP_STATUS_DEFAULT_TTL
        assert webui_module._SETUP_STATUS_DEFAULT_TTL > 0

    def test_malformed_ttl_env_falls_back_to_the_default(self, monkeypatch):
        from dourmouse import webui as webui_module

        monkeypatch.setenv("DOURMOUSE_SETUP_CACHE_TTL", "not-a-number")
        assert webui_module._setup_status_cache_ttl() == webui_module._SETUP_STATUS_DEFAULT_TTL


# --------------------------------------------------------------------------- #
# A5 — Codex backend
# --------------------------------------------------------------------------- #

class TestCodexBackend:
    def test_codex_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from dourmouse.code_backends import load_backend

        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            load_backend("codex")

    def test_codex_resolves_with_key(self, monkeypatch):
        monkeypatch.setenv("CODEX_API_KEY", "sk-test-1234567890abcdef")
        from dourmouse.code_backends import load_backend

        base, key, model = load_backend("codex")
        assert base == "https://api.openai.com/v1"
        assert key == "sk-test-1234567890abcdef"
        assert model == "gpt-5-codex"

    def test_codex_accepts_openai_key(self, monkeypatch):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-alt-1234567890abcdef")
        from dourmouse.code_backends import load_backend

        _base, key, _model = load_backend("codex")
        assert key == "sk-alt-1234567890abcdef"


# --------------------------------------------------------------------------- #
# A6 — Gmail module (honest NOT CONFIGURED)
# --------------------------------------------------------------------------- #

class TestGoogleServices:
    def test_not_configured_without_env(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        from dourmouse import google_services as gs

        # Hermetic: pin the local_secrets fallback to empty too, so the test
        # never depends on what a user has typed into that gitignored file.
        monkeypatch.setattr(gs, "_local_secrets", dict)
        assert gs.gmail_configured() is False
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            gs.gmail_search("test")
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            gs.gmail_send("a@b.com", "hi", "body")

    def test_configured_when_env_present(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        assert gs.gmail_configured() is True
        assert gs.status()["configured"] is True
        assert "(via env)" in gs.status()["detail"]

    def test_configured_via_local_secrets(self, monkeypatch):
        """v5.1: single-user source-tree credentials work with NO env vars."""
        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        from dourmouse import google_services as gs

        monkeypatch.setattr(
            gs, "_local_secrets", lambda: {"user": "local@gmail.com", "password": "1234567890abcdef"}
        )
        assert gs.gmail_configured() is True
        assert "local@gmail.com" in gs.status()["detail"]
        assert "(via local_secrets.py)" in gs.status()["detail"]

    def test_env_wins_over_local_secrets(self, monkeypatch):
        """v5.1: env vars always beat the local_secrets fallback."""
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "env@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        monkeypatch.setattr(
            gs, "_local_secrets", lambda: {"user": "local@gmail.com", "password": "deadbeef12345678"}
        )
        assert gs._user() == "env@gmail.com"
        assert gs._app_password() == "abcdefghijklmnop"

    def test_missing_local_secrets_module_is_empty(self, monkeypatch):
        """v5.1: an absent/broken local_secrets file degrades to {}."""
        from dourmouse import google_services as gs

        monkeypatch.setattr(gs, "_local_secrets", dict)
        assert gs._user() == ""
        assert gs._app_password() == ""
        assert gs.gmail_configured() is False

    def test_local_secrets_absent_module_guard(self, monkeypatch):
        """v5.1: the REAL import guard — a machine without the gitignored
        file (fresh checkout / any-device install) must degrade to NOT
        CONFIGURED, never crash gmail_configured (reviewer-caught)."""
        import sys

        monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
        monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
        # Simulate the file being absent: block the module import entirely.
        monkeypatch.setitem(sys.modules, "dourmouse.local_secrets", None)
        from dourmouse import google_services as gs

        assert gs._local_secrets() == {}
        assert gs.gmail_configured() is False

    def test_send_validates_input_before_network(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "abcdefghijklmnop")
        from dourmouse import google_services as gs

        assert "ERROR" in gs.gmail_send("", "subj", "body")
        assert "ERROR" in gs.gmail_send("a@b.com", "", "body")

    def test_calendar_honest_not_configured(self):
        from dourmouse import google_services as gs

        assert "NOT CONFIGURED" in gs.calendar_events()


# --------------------------------------------------------------------------- #
# A4 — fast dispatch model
# --------------------------------------------------------------------------- #

class TestFastDispatchModel:
    def test_orchestrator_defaults_to_fast_model(self, monkeypatch):
        """v5.2: the fast-dispatch default is qwen2.5:7b (answers directly;
        qwen3:4b ignored think=False and rambled — measured live)."""
        monkeypatch.delenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", raising=False)
        from dourmouse.config import load_ollama_config

        cfg = load_ollama_config()
        assert cfg.model_for_agent("orchestrator") == "qwen2.5:7b"
        # heavy agents stay on the big model
        assert cfg.model_for_agent("research_info") == cfg.model
        # unknown agents fall back to the default model
        assert cfg.model_for_agent("nope") == cfg.model

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_OLLAMA_MODEL_ORCHESTRATOR", "qwen3:8b")
        from dourmouse.config import load_ollama_config

        cfg = load_ollama_config()
        assert cfg.model_for_agent("orchestrator") == "qwen3:8b"


# --------------------------------------------------------------------------- #
# A2 — read_upload tool (sandbox)
# --------------------------------------------------------------------------- #

class TestReadUploadTool:
    def test_upload_readable_by_system_agent(self, monkeypatch, tmp_path):
        from dourmouse.system_access import _uploads_root, build_system_subagent

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        root = _uploads_root()
        (root / "data.csv").write_text("a,b,c\n1,2,3\n")
        sub = build_system_subagent()
        spec = next(t for t in sub.tools if t.name == "read_upload")
        out = spec.handler({"name": "data.csv"})
        assert "a,b,c" in out

    def test_upload_rejects_path_escape(self, monkeypatch, tmp_path):
        from dourmouse.system_access import build_system_subagent

        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        sub = build_system_subagent()
        spec = next(t for t in sub.tools if t.name == "read_upload")
        # A path-like name is refused before it can resolve anywhere (the
        # first guard rejects any path separators — honest refusal either way).
        out = spec.handler({"name": "../.env"})
        assert "ERROR" in out or "REFUSED" in out
        assert "ERROR" in spec.handler({"name": "missing.txt"})
