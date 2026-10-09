"""FIX-R R-14: bulk or vague deletion keeps the list-and-confirm step in chat; a single named file does not need it."""

from __future__ import annotations

from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS
from dourmouse.general_roster import build_general_registry
from dourmouse.dispatch import Permission


def _admin() -> str:
    return " ".join(AGENT_SYSTEM_PROMPTS["admin_ops"].split())  # the prompt is hard-wrapped: compare words, not line breaks


def test_the_prompt_no_longer_tells_the_model_to_delete_a_scope_without_asking_in_chat():
    text = _admin()
    assert "so do not ask in chat first" not in text
    assert "which is the confirmation; do not ask in chat first" not in text


def test_the_prompt_asks_for_the_list_and_a_yes_for_several_files_or_a_vague_scope():
    text = _admin()
    assert "post the complete list in chat and wait for the user's explicit yes" in text
    assert '"clean up"' in text and "approvals switched to automatic" in text
    assert "wait for the user's explicit yes to the list in chat" in text
    assert "a deletion of more than one file or of an unnamed scope needs the user's yes to the list in chat first" in text


def test_a_single_named_file_may_go_straight_to_the_card():
    text = _admin()
    assert "if the user named exactly one file" in text and "its approval card shows the owner that file" in text


def test_the_earlier_fix_still_holds():
    text = _admin()
    assert "Call [delete_file] only after confirmation" not in text
    assert "Do not call [delete_file] until the user has explicitly confirmed" not in text
    assert "Clean this folder" in text  # the sentence that says a vague request is no confirmation is still there


def test_delete_file_is_still_a_gated_tool():
    spec = next(t for s in build_general_registry().all_subagents() for t in s.tools if t.name == "delete_file")
    assert spec.permission is Permission.REQUIRES_CONFIRMATION
