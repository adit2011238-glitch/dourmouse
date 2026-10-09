"""FR fix P5-36: a malformed tool call must come back as an error result."""

from __future__ import annotations

import pytest

from dourmouse.orchestrator import dispatch
from dourmouse.tests.test_orchestrator import FakeClient, _FakeMessage, _FakeResponse, _FakeToolCall


@pytest.mark.parametrize("arguments", ["{}", "[]", "null", '"x"'])
def test_run_atlas_research_with_missing_or_malformed_arguments_is_an_error_result(arguments):
    call = _FakeToolCall("c1", "run_atlas_research", arguments)
    client = FakeClient([
        _FakeResponse(_FakeMessage(content=None, tool_calls=[call])),
        _FakeResponse(_FakeMessage(content="could not run it")),
    ])
    report = dispatch("Run ATLAS research.", client=client)
    assert report["final_text"] == "could not run it"
    result = next(t for t in report["transcript"] if t["type"] == "tool_result")
    assert result["text"].startswith("ERROR")
