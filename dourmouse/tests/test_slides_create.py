"""Pin the Slides batchUpdate request shape.

Regression: the deck builder sent `insertLayout`, which is not a Slides API
request type. presentations.create succeeded first, so every call left an
empty presentation in the user's Drive and then reported a 400. The API
rejected it on name alone, so only a shape assertion catches it.
"""

from __future__ import annotations

import pytest

from dourmouse import google_services as gs

SLIDES = [
    {"title": "First", "body": "one"},
    {"title": "Second", "body": "two"},
]

# Request types the Slides v1 batchUpdate endpoint actually accepts.
# Verified against live API rejections: "insertLayout" and "createTextBox"
# both look plausible and neither exists. A text box is createShape with
# shapeType TEXT_BOX.
VALID_REQUEST_KEYS = {
    "createSlide", "deleteObject", "createShape",
    "insertText", "updateTextStyle", "updateShapeProperties",
    "updateParagraphStyle", "createImage", "replaceAllText",
}


@pytest.fixture
def calls(monkeypatch):
    """Capture batchUpdate payloads; fake a successful create."""
    seen: list[dict] = []

    def fake_http_json(method, url, token, body=None):
        seen.append({"url": url, "body": body})
        if url.endswith(":batchUpdate"):
            return {"replies": []}
        return {"presentationId": "PRES1", "slides": [{"objectId": "p"}]}

    monkeypatch.setattr(gs, "_oauth_access_token", lambda: "tok")
    monkeypatch.setattr(gs, "_http_json", fake_http_json)
    return seen


def _batches(calls):
    return [c["body"]["requests"] for c in calls if c["url"].endswith(":batchUpdate")]


def test_layout_batch_uses_createSlide_not_insertLayout(calls):
    gs.slides_create("Deck", SLIDES)
    layout = _batches(calls)[0]
    keys = {k for r in layout for k in r}

    assert "insertLayout" not in keys, "insertLayout is not a Slides API request type"
    assert "createSlide" in keys


def test_every_request_key_is_a_real_api_type(calls):
    gs.slides_create("Deck", SLIDES)
    for batch in _batches(calls):
        for req in batch:
            for key in req:
                assert key in VALID_REQUEST_KEYS, f"unknown request type {key!r}"


def test_createSlide_carries_a_layout_reference(calls):
    gs.slides_create("Deck", SLIDES)
    for req in _batches(calls)[0]:
        if "createSlide" in req:
            ref = req["createSlide"]["slideLayoutReference"]
            assert "predefinedLayout" in ref


def test_blank_layout_so_placeholders_do_not_sit_under_the_text_boxes(calls):
    gs.slides_create("Deck", SLIDES)
    for req in _batches(calls)[0]:
        if "createSlide" in req:
            assert req["createSlide"]["slideLayoutReference"]["predefinedLayout"] == "BLANK"


def test_one_slide_created_per_requested_slide_in_order(calls):
    gs.slides_create("Deck", SLIDES)
    creates = [r["createSlide"] for r in _batches(calls)[0] if "createSlide" in r]

    assert len(creates) == len(SLIDES)
    assert [c["insertionIndex"] for c in creates] == [0, 1]
    assert [c["objectId"] for c in creates] == ["slide_1", "slide_2"]


def test_default_blank_slide_is_removed(calls):
    gs.slides_create("Deck", SLIDES)
    assert any("deleteObject" in r for r in _batches(calls)[0])


def test_text_boxes_use_createShape_not_createTextBox(calls):
    """createTextBox is not a Slides API request type either."""
    gs.slides_create("Deck", SLIDES)
    keys = {k for r in _batches(calls)[1] for k in r}
    assert "createTextBox" not in keys
    assert "createShape" in keys
    for r in _batches(calls)[1]:
        if "createShape" in r:
            assert r["createShape"]["shapeType"] == "TEXT_BOX"


def test_text_is_written_onto_the_created_slides(calls):
    gs.slides_create("Deck", SLIDES)
    text_batch = _batches(calls)[1]
    inserted = [r["insertText"]["text"] for r in text_batch if "insertText" in r]

    assert "First" in inserted and "one" in inserted
    assert "Second" in inserted and "two" in inserted

    # boxes must be anchored to the slides created in the first batch
    pages = {
        r["createShape"]["elementProperties"]["pageObjectId"]
        for r in text_batch if "createShape" in r
    }
    assert pages <= {"slide_1", "slide_2"}


def test_empty_slide_list_still_reports_honestly(calls):
    """No slides asked for: the default blank page is still removed, and the
    result says plainly that the deck has none rather than implying content."""
    out = gs.slides_create("Deck", [])

    assert "0 slide" in out
    layout = _batches(calls)[0]
    assert all("createSlide" not in r for r in layout)
    assert any("deleteObject" in r for r in layout)


def test_missing_title_is_refused(calls):
    out = gs.slides_create("   ", SLIDES)
    assert out.startswith("ERROR")
    assert not calls


def test_result_carries_the_openable_url(calls):
    out = gs.slides_create("Deck", SLIDES)
    assert "PRES1" in out
    assert "docs.google.com/presentation" in out
