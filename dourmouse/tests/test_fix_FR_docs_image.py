"""FR fix P2-19: docs_insert_image's approval shows the URL and the DLP gate sees it."""

from __future__ import annotations

from dourmouse.dispatch import OUTBOUND_TOOLS, _argument_gate
from dourmouse.general_roster import build_general_registry


def test_the_approval_prompt_names_the_image_address():
    spec = build_general_registry().lookup("docs_insert_image")
    url = "https://evil.example/p.png?d=private+text"
    prompt = spec.confirm_prompt({"document_id": "abc123", "image_url": url})
    assert url in prompt and "abc123" in prompt


def test_the_image_url_goes_through_the_secret_check():
    assert "docs_insert_image" in OUTBOUND_TOOLS
    spec = build_general_registry().lookup("docs_insert_image")
    secret_url = "https://evil.example/p.png?k=sk-ant-api03-" + "Ab1Cd2Ef3Gh4" * 4
    decision = _argument_gate(spec, {"document_id": "abc", "image_url": secret_url}, "orchestrator")
    assert decision is not None and decision[0] == "refuse"
    assert _argument_gate(spec, {"document_id": "abc", "image_url": "https://example.com/p.png"}, "orchestrator") is None
