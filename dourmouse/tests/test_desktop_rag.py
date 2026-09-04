"""Tests for the desktop spatial-vault RAG bridge (dourmouse/desktop_rag.py).

Every real subprocess is replaced with an injected fake ``runner`` — see
``Runner`` in the module under test. No test in this file touches the
network or the real desktop; the live end-to-end proof (a real query
against the real vault, returning real Wikipedia/HuggingFace rows with
verify_cosine == 1.0) was run separately and is recorded in the module's own
docstring and in the commit that lands this file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dourmouse.desktop_rag import (
    DesktopRagError,
    desktop_available,
    desktop_rag_config,
    desktop_rag_configured,
    desktop_rag_status,
    format_desktop_rag,
    query_desktop_rag,
)

_ENV_KEYS = (
    "DOURMOUSE_DESKTOP_RAG_HOST",
    "DOURMOUSE_DESKTOP_RAG_USER",
    "DOURMOUSE_DESKTOP_RAG_KEY",
    "DOURMOUSE_DESKTOP_RAG_DB",
    "DOURMOUSE_DESKTOP_RAG_INDEX",
    "DOURMOUSE_DESKTOP_RAG_TABLE",
    "DOURMOUSE_DESKTOP_RAG_MODEL",
    "DOURMOUSE_DESKTOP_RAG_PYTHON",
    "DOURMOUSE_DESKTOP_RAG_ID_FILTER_SQL",
    "DOURMOUSE_DESKTOP_RAG_ID_ORDER_SQL",
    "DOURMOUSE_DESKTOP_RAG_TIMEOUT",
    "DOURMOUSE_DESKTOP_RAG_PROBE_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No test may inherit this machine's real .env values -- otherwise
    "not configured" tests would silently pass for the wrong reason."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_HOST", "100.98.97.23")
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_USER", "ankit")
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_KEY", "/fake/key")


def _sentinel_line(payload: dict) -> str:
    from dourmouse.desktop_rag import _SENTINEL

    return f"noise before\n{_SENTINEL}{json.dumps(payload)}\nnoise after"


def _fake_runner(returncode=0, stdout="", stderr=""):
    def run(cmd, timeout, stdin_path=None):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


class TestConfiguration:
    def test_not_configured_with_no_env_at_all(self):
        assert desktop_rag_configured() is False
        assert desktop_rag_config() is None

    def test_configured_only_once_all_three_required_vars_are_set(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_HOST", "h")
        assert desktop_rag_configured() is False  # user, key still missing
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_USER", "u")
        assert desktop_rag_configured() is False  # key still missing
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_KEY", "/k")
        assert desktop_rag_configured() is True

    def test_defaults_match_the_live_verified_vault_shape(self, configured_env):
        cfg = desktop_rag_config()
        assert cfg is not None
        assert cfg["table"] == "hybrid_chunks"
        assert cfg["model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert "HuggingFace_Parquet_Stream" in cfg["id_filter_sql"]
        assert "English_Wikipedia" in cfg["id_filter_sql"]
        # Pristine_Filtered_Stream (797,689 rows) is genuinely NOT embedded --
        # a default that silently included it would be a fabricated mapping.
        assert "Pristine_Filtered_Stream" not in cfg["id_filter_sql"]

    def test_overrides_are_honoured(self, configured_env, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_TABLE", "custom_table")
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_TIMEOUT", "999")
        cfg = desktop_rag_config()
        assert cfg["table"] == "custom_table"
        assert cfg["timeout"] == 999

    def test_an_unparseable_timeout_falls_back_rather_than_crashing(self, configured_env, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_TIMEOUT", "not-a-number")
        cfg = desktop_rag_config()
        assert cfg["timeout"] == 150  # the documented default


class TestQueryDesktopRagNotConfigured:
    def test_raises_not_configured_never_returns_empty_silently(self):
        """An empty list would look identical to 'searched, found nothing' --
        that distinction is load-bearing (Rule 2.2), so this must raise."""
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("anarchism")
        assert exc.value.kind == "NOT_CONFIGURED"


class TestQueryDesktopRagValidation:
    def test_empty_query_is_refused_before_any_ssh_call(self, configured_env):
        calls = []

        def spy(cmd, timeout, stdin_path=None):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("   ", runner=spy)
        assert exc.value.kind == "BAD_REQUEST"
        assert calls == []  # never touched the network for a bad request

    def test_non_positive_limit_is_refused(self, configured_env):
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", limit=0, runner=_fake_runner())
        assert exc.value.kind == "BAD_REQUEST"


class TestQueryDesktopRagRealShape:
    def test_a_successful_query_returns_the_real_hits_list(self, configured_env):
        hits = [
            {
                "id": 1,
                "title": "Anarchism",
                "chunk_text": "Anarchism is a political philosophy...",
                "source_pipeline": "HuggingFace_Parquet_Stream",
                "score": 0.749,
                "distance": 0.502,
                "position": 0,
                "verify_cosine": 1.0,
            }
        ]
        stdout = _sentinel_line({"ok": True, "hits": hits})
        result = query_desktop_rag("anarchism", runner=_fake_runner(stdout=stdout))
        assert result == hits

    def test_a_remote_ok_false_response_raises_its_real_kind_and_detail(self, configured_env):
        stdout = _sentinel_line(
            {"ok": False, "kind": "MAPPING_MISMATCH", "detail": "cosine 0.41 at position 5"}
        )
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=_fake_runner(stdout=stdout))
        assert exc.value.kind == "MAPPING_MISMATCH"
        assert "0.41" in str(exc.value)

    def test_a_non_list_hits_field_is_a_bad_response_not_a_silent_empty_list(self, configured_env):
        stdout = _sentinel_line({"ok": True, "hits": "not-a-list"})
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=_fake_runner(stdout=stdout))
        assert exc.value.kind == "BAD_RESPONSE"

    def test_ssh_failure_with_no_sentinel_reports_the_real_exit_and_stderr(self, configured_env):
        run = _fake_runner(returncode=255, stdout="", stderr="Permission denied (publickey).")
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=run)
        assert exc.value.kind == "UNREACHABLE"
        assert "Permission denied" in str(exc.value)

    def test_a_timeout_is_reported_as_timeout_not_unreachable(self, configured_env):
        import subprocess

        def timing_out(cmd, timeout, stdin_path=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=timing_out)
        assert exc.value.kind == "TIMEOUT"

    def test_garbage_on_the_sentinel_line_is_a_bad_response(self, configured_env):
        from dourmouse.desktop_rag import _SENTINEL

        stdout = f"{_SENTINEL}{{not valid json"
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=_fake_runner(stdout=stdout))
        assert exc.value.kind == "BAD_RESPONSE"

    def test_no_sentinel_line_at_all_is_a_bad_response(self, configured_env):
        with pytest.raises(DesktopRagError) as exc:
            query_desktop_rag("x", runner=_fake_runner(returncode=0, stdout="ok but no marker"))
        assert exc.value.kind == "BAD_RESPONSE"


class TestDesktopAvailable:
    def test_false_when_not_configured(self):
        assert desktop_available() is False

    def test_true_on_a_reachable_probe(self, configured_env):
        # desktop_available runs a plain `ssh ... echo DOURMOUSE_OK` -- no
        # Python, no sentinel JSON, on the real far side. See the module.
        assert desktop_available(runner=_fake_runner(stdout="DOURMOUSE_OK\n")) is True

    def test_false_rather_than_raising_on_a_probe_failure(self, configured_env):
        """A cheap reachability check must never itself raise into a chat
        turn -- that is the whole reason it exists as a separate function
        from query_desktop_rag."""
        assert desktop_available(runner=_fake_runner(returncode=255)) is False


class TestDesktopRagStatus:
    def test_shape_matches_connections_check_connections(self):
        status = desktop_rag_status()
        assert set(status.keys()) == {"ok", "detail", "hint"}

    def test_not_configured_status_is_honest(self):
        status = desktop_rag_status()
        assert status["ok"] is False
        assert "not" in status["detail"].lower() or "NOT_CONFIGURED" in status["detail"]

    def test_reachable_status_reports_real_vault_facts(self, configured_env):
        stdout = _sentinel_line(
            {
                "ok": True,
                "max_id": 1023765,
                "index_mb": 331,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            }
        )
        status = desktop_rag_status(runner=_fake_runner(stdout=stdout))
        assert status["ok"] is True
        assert "1023765" in status["detail"] or "331" in status["detail"]


class TestFormatDesktopRag:
    """format_desktop_rag(query, limit, runner) runs the full pipeline
    itself and renders the result as text -- it does not take a
    pre-fetched hit list."""

    def test_not_configured_is_a_visible_honest_line_not_an_exception(self):
        text = format_desktop_rag("anarchism")
        assert text.startswith("NOT CONFIGURED:")

    def test_zero_hits_says_so_plainly_not_a_fabricated_match(self, configured_env):
        stdout = _sentinel_line({"ok": True, "hits": []})
        text = format_desktop_rag("x", runner=_fake_runner(stdout=stdout))
        assert "no matches" in text.lower()

    def test_real_hits_render_with_their_source(self, configured_env):
        hits = [
            {
                "id": 1,
                "title": "Anarchism",
                "chunk_text": "x" * 500,
                "source_pipeline": "HuggingFace_Parquet_Stream",
                "score": 0.749,
            }
        ]
        stdout = _sentinel_line({"ok": True, "hits": hits})
        text = format_desktop_rag("x", runner=_fake_runner(stdout=stdout))
        assert "Anarchism" in text
        assert "HuggingFace_Parquet_Stream" in text

    def test_a_remote_failure_is_a_visible_line_never_a_raise(self, configured_env):
        text = format_desktop_rag("x", runner=_fake_runner(returncode=255, stderr="no route to host"))
        assert "UNAVAILABLE" in text
