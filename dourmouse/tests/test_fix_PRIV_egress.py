"""PRIV-4 (h): every backend path writes one egress row per request, metadata only.

Ollama Cloud: test_fix_PRIV_gate.py::test_d_*. Claude CLI: test_fix_PRIV_bridge.py. Gemini:
test_fix_PRIV_no_local_routing.py::test_p531_gemini_call_writes_an_egress_row. Here: Codex CLI,
the OpenAI-compatible coding backends, and a plain OpenAI-compatible chat client (NVIDIA,
FreeLLMAPI, OmniRoute) in the dispatch loop.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

from dourmouse import code_backends, dispatch, privacy

SECRET = "the quarterly numbers are bad"


def _only_row() -> dict[str, Any]:
    rows = privacy.read_egress(10)
    assert len(rows) == 1, rows
    raw = privacy.egress_path().read_text(encoding="utf-8")
    assert "quarterly" not in raw
    return rows[0]


def test_codex_cli_records(monkeypatch):
    monkeypatch.setattr("dourmouse.general_roster._find_codex_cli", lambda: "/usr/bin/codex")
    monkeypatch.setattr("dourmouse.mcp_bridge.ensure_codex_mcp_registered", lambda cli: None)
    monkeypatch.setattr(code_backends.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="done", stderr=""))
    code_backends._run_codex(SECRET, cwd=None, timeout=5)
    row = _only_row()
    assert row["backend"] == "codex_cli" and row["provider"] == "Codex CLI (OpenAI)"


def test_openai_compatible_coding_backend_records(monkeypatch):
    msg = SimpleNamespace(content="code")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=msg)]))))
    monkeypatch.setattr(code_backends, "_openai_client_factory", lambda base_url, api_key: fake)
    code_backends._run_openai_compat("https://integrate.api.nvidia.com/v1", "k", "m", SECRET, 5)
    row = _only_row()
    assert row["backend"] == "nvidia" and row["model"] == "m"


def test_openai_compatible_chat_client_records_in_the_loop(monkeypatch):
    from openai import OpenAI

    client = OpenAI(api_key="k", base_url="http://127.0.0.1:3000/v1")
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))])
    monkeypatch.setattr(client.chat.completions, "create", lambda **kw: reply)
    with privacy.turn_scope("tab-oa"):
        dispatch._call_with_retry(client, model="free-model", messages=[{"role": "user", "content": SECRET}],
                                  tools=[], config=None)
    row = _only_row()
    assert row["backend"] == "local_proxy" and row["chat"]


def test_adapters_are_not_recorded_twice(monkeypatch):
    """The loop records only plain OpenAI clients; Ollama, Gemini and the
    Claude CLI record where they build their own request."""
    class _NotOpenAI:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None))])))

    dispatch._call_with_retry(_NotOpenAI(), model="m", messages=[], tools=[], config=None)
    assert privacy.read_egress(10) == []


def test_rows_are_json_lines_and_owner_only_file():
    import os
    import stat

    privacy.record_egress(backend="gemini", model="g", text=SECRET)
    path = privacy.egress_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_ollama_cloud_with_no_key_sends_nothing():
    """H-PRIV-3: _ollama_cloud_config() with no OLLAMA_API_KEY built a client
    that still POSTed the whole request (the security analyst's findings at
    start-up, found live) to ollama.com, only to get a 401 back."""
    import pytest

    from dourmouse.config import OllamaConfig

    posted: list[Any] = []
    client = dispatch.OllamaNativeClient(OllamaConfig(base_url="https://ollama.com/v1", model="m", api_key=""),
                                         _post=lambda payload: posted.append(payload) or "{}")
    with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": SECRET}])
    assert posted == [] and privacy.read_egress(5) == []
