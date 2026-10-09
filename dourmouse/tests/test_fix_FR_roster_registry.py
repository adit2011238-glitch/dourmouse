"""FR fix P2-14 (what FS2's MCP cache left open): self-extension modules were
exec'd again on every build_general_registry()."""

from __future__ import annotations

import pytest

from dourmouse import general_roster as gr
from dourmouse import self_extensions as se

_PARAMS = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
_HANDLER = "def handle(arguments):\n    return str(arguments.get('text', ''))[::-1]\n"
_TEST = (
    "from dourmouse.self_extensions import load_approved\n"
    "def test_it():\n    assert load_approved('reverse_text').handle({'text': 'ab'}) == 'ba'\n"
)


@pytest.fixture()
def approved(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    gr._APPROVED_SPEC_CACHE.clear()
    store = se.SelfExtensions()
    entry = store.add_draft(
        capability_gap="gap", tool_name="reverse_text", description="Reverses text.",
        parameters_schema=_PARAMS, handler_source=_HANDLER, test_source=_TEST,
    )
    assert se.approve(entry["id"], store=store)["ok"] is True
    yield tmp_path
    gr._APPROVED_SPEC_CACHE.clear()


def test_an_unchanged_approved_module_is_executed_once_per_process(approved, monkeypatch):
    calls = []
    real = se.load_approved
    monkeypatch.setattr(se, "load_approved", lambda *a, **k: calls.append(a) or real(*a, **k))
    gr._APPROVED_SPEC_CACHE.clear()
    for _ in range(4):
        registry = gr.build_general_registry()
        assert registry.lookup("reverse_text").handler({"text": "abc"}) == "cba"
    assert len(calls) == 1


def test_a_module_edited_after_approval_is_refused_not_served_from_the_cache(approved):
    gr.build_general_registry()  # caches the good module
    path = approved / "self_extensions" / "approved" / "reverse_text.py"
    path.write_text(path.read_text() + "\n# planted\n", encoding="utf-8")
    registry = gr.build_general_registry()
    assert registry.lookup("reverse_text") is None  # sha mismatch: refused exactly as before


def test_an_unapproved_copy_of_cached_bytes_is_not_served(approved, tmp_path_factory, monkeypatch):
    gr.build_general_registry()
    other = tmp_path_factory.mktemp("other_ws")
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(other))
    d = other / "self_extensions" / "approved"
    d.mkdir(parents=True)
    (d / "reverse_text.py").write_bytes((approved / "self_extensions" / "approved" / "reverse_text.py").read_bytes())
    registry = gr.build_general_registry()  # same bytes, but nobody approved them here
    assert registry.lookup("reverse_text") is None
