"""FD2 fix P3-39: the briefing never probes or describes a desktop vault that is not configured."""

from __future__ import annotations

from dourmouse import desktop_rag, model_context


def _clear(monkeypatch):
    for n in ("HOST", "USER", "KEY"):
        monkeypatch.delenv(f"DOURMOUSE_DESKTOP_RAG_{n}", raising=False)


def test_unconfigured_desktop_adds_no_line_and_never_probes(monkeypatch):
    _clear(monkeypatch)

    calls = []

    def probe(*a, **k):
        calls.append(1)
        return {"ok": False, "detail": "x"}

    monkeypatch.setattr(desktop_rag, "desktop_rag_status", probe)
    lines = model_context._rag_lines()
    assert calls == []  # no SSH probe when nothing is configured
    assert not any("desktop vault" in ln for ln in lines)


def test_configured_desktop_line_uses_the_real_host_and_no_hardcoded_count(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_HOST", "vault.example")
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_USER", "u")
    monkeypatch.setenv("DOURMOUSE_DESKTOP_RAG_KEY", "/tmp/k")
    monkeypatch.setattr(desktop_rag, "desktop_rag_status", lambda: {"ok": False, "detail": "ssh timed out"})
    text = "\n".join(model_context._rag_lines())
    assert "vault.example" in text
    assert "UNAVAILABLE" in text and "ssh timed out" in text
    assert "1,023,765" not in text
    assert "Windows" not in text
