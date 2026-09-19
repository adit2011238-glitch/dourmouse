"""RealBrain tests: hermetic (dourmouse.chat.ChatSession replaced with a
scripted fake, same monkeypatch pattern as test_goal_runtime.py's own
_FakeSession) but exercising the REAL prompt construction, REAL citation
parsing, and the REAL exam-engine citation gate end to end -- only the
model call itself is faked, exactly like every other independent-reasoning
seam in this codebase (_verify_completion, _verify_goal_criteria).
"""

from __future__ import annotations

import dourmouse.chat as chat_module

from ..brain import RealBrain
from ..core import AgentRecord, Status
from ..pipeline import QualificationPipeline
from ..store import AgentStore


class _FakeSession:
    """Records every ask() call; returns scripted final_text in order, the
    last one repeating for any further calls (mirrors general_roster.py's
    tests' own FakeClient "repeat once one is left" convention)."""

    calls: list[str] = []
    responses: list[object] = []

    def __init__(self, registry, session_file=None):
        pass

    def ask(self, prompt, force_plain_dispatch=False):
        type(self).calls.append(prompt)
        responses = type(self).responses
        scripted = responses[0] if len(responses) == 1 else responses.pop(0)
        if callable(scripted):
            scripted = scripted()
        return {"final_text": scripted}


def _install(monkeypatch, responses: list[object]):
    _FakeSession.calls = []
    _FakeSession.responses = list(responses)
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


def test_study_extracts_real_text_and_returns_learned_dict(tmp_path) -> None:
    src = tmp_path / "paper.pdf"
    src.write_text("group theory homomorphisms kernels")
    brain = RealBrain()
    learned = brain.study([src], ["Algebra"])
    assert learned == {"paper.pdf": "group theory homomorphisms kernels"}
    assert brain._facts["paper.pdf"] == "group theory homomorphisms kernels"
    assert "Algebra" in brain._concepts


def test_answer_sends_studied_context_and_parses_citations(tmp_path, monkeypatch) -> None:
    src = tmp_path / "paper.pdf"
    src.write_text("homomorphisms kernels quotient groups")
    brain = RealBrain()
    brain.study([src], ["Algebra"])
    _install(monkeypatch, [
        "The kernel is the preimage of the identity.\nCITATIONS: paper.pdf",
    ])

    ans = brain.answer("Solve the problems in 'paper.pdf'")

    assert ans.citations == ("paper.pdf",)
    assert "kernel is the preimage" in ans.text
    # The real studied text and the real question both reached the model.
    prompt = _FakeSession.calls[0]
    assert "homomorphisms kernels quotient groups" in prompt
    assert "Solve the problems in 'paper.pdf'" in prompt


def test_answer_with_no_citations_line_is_honest_empty(monkeypatch) -> None:
    brain = RealBrain()
    _install(monkeypatch, ["I don't have enough information to answer this."])
    ans = brain.answer("Solve the problems in 'unseen.pdf'")
    assert ans.citations == ()


def test_answer_none_citations_line_is_empty_not_a_literal_string(monkeypatch) -> None:
    brain = RealBrain()
    _install(monkeypatch, ["No relevant material studied.\nCITATIONS: none"])
    ans = brain.answer("question")
    assert ans.citations == ()


def test_answer_strips_and_splits_multiple_citations(monkeypatch) -> None:
    brain = RealBrain()
    _install(monkeypatch, ["Combined answer.\nCITATIONS: a.pdf, b.pdf ,  c.pdf"])
    ans = brain.answer("question")
    assert ans.citations == ("a.pdf", "b.pdf", "c.pdf")


def test_broken_model_call_fails_honestly_not_a_crash(monkeypatch) -> None:
    class _RaisingSession:
        def __init__(self, registry, session_file=None):
            pass

        def ask(self, prompt, force_plain_dispatch=False):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(chat_module, "ChatSession", _RaisingSession)
    brain = RealBrain()
    ans = brain.answer("question")
    assert ans.citations == ()
    assert "brain call failed" in ans.text
    assert "network unreachable" in ans.text


def test_study_context_caps_and_keeps_most_recent(tmp_path) -> None:
    brain = RealBrain()
    from .. import brain as brain_module

    small_cap = 500
    monkey_cap = small_cap
    original_cap = brain_module._STUDY_CONTEXT_CAP_CHARS
    brain_module._STUDY_CONTEXT_CAP_CHARS = monkey_cap
    try:
        old = tmp_path / "old.pdf"
        old.write_text("OLDMARKER " * 100)
        new = tmp_path / "new.pdf"
        new.write_text("NEWMARKER " * 100)
        brain.study([old], [])
        brain.study([new], [])
        context = brain._studied_context()
        assert len(context) <= monkey_cap + len("[earlier studied material truncated]\n")
        assert "NEWMARKER" in context
        assert "OLDMARKER" not in context  # trimmed: oldest content drops first
    finally:
        brain_module._STUDY_CONTEXT_CAP_CHARS = original_cap


def test_remediate_reingests_without_a_model_call(tmp_path, monkeypatch) -> None:
    src = tmp_path / "paper.pdf"
    src.write_text("corrected material")
    brain = RealBrain()
    _install(monkeypatch, ["should not be called"])
    brain.remediate("you missed the kernel definition", [src])
    assert brain._facts["paper.pdf"] == "corrected material"
    assert _FakeSession.calls == []  # remediation is real re-ingestion, no LLM call


def test_end_to_end_pipeline_qualifies_with_real_brain_and_real_citation_gate(
    tmp_path, monkeypatch,
) -> None:
    """The real QualificationPipeline, the real exam engine's citation gate,
    and RealBrain wired together -- only the model call is faked. Proves the
    seam works, not just RealBrain in isolation."""
    folder = tmp_path / "Mathematics" / "Algebra"
    (folder / "tests").mkdir(parents=True)
    (folder / "keys").mkdir()
    (folder / "tests" / "qual_2023.pdf").write_text(
        "PhD qualifying exam 2023. homomorphisms kernels quotient groups.")
    (folder / "keys" / "qual_2023.pdf").write_text("homomorphisms kernels quotient groups")

    from ..study import load_corpus
    corpus = load_corpus(tmp_path, "Mathematics", "Algebra")

    _install(monkeypatch, [
        # First attempt: held out, brain honestly cannot answer -> fails.
        "I have not studied this paper yet.",
        # After remediation (paper now allowed), brain answers correctly
        # with a real citation naming the real corpus file.
        "homomorphisms kernels quotient groups\nCITATIONS: qual_2023.pdf",
    ])

    store = AgentStore(tmp_path / "q.db")
    rec = AgentRecord(domain="Mathematics", field="Algebra")
    pipe = QualificationPipeline(store, RealBrain(), corpus)
    final = pipe.run(rec)

    assert final is Status.QUALIFIED
    assert rec.passed_iterations == ("qual_2023.pdf",)
    fail, ok = rec.history
    assert not fail.passed and fail.citations_verified is False
    assert ok.passed and ok.citations_verified is True
