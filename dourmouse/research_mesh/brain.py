"""Brain interface: the seam between the pipeline and whatever reasons for the agent.

Design (why this exists):

- The qualification pipeline must be testable and runnable *without* an LLM.
  Every reasoning capability -- studying sources, answering exam questions,
  remediating after feedback -- goes through this interface, so tests inject a
  deterministic MockBrain and the pilot can dry-run the whole loop offline.
- A real backend plugs in by implementing `Brain`; nothing else in the
  pipeline changes. That keeps the pipeline decoupled from any vendor, exactly
  like the project's fetch/clock injection conventions.
- `BrainNotConfigured` is raised (never silently swallowed) when no backend is
  configured, so the system is honest about what it cannot do -- the same
  fail-closed discipline as the rest of the codebase.

The MockBrain models an agent whose knowledge is *exactly* the sources it was
given to study: it answers from learned facts keyed by source filename. This
makes the anti-cheat rule directly testable -- a mock that studied only
non-held-out papers cannot answer a held-out exam.

RealBrain (2026-09-19, the rebuild of the orphaned jarvis/research_mesh
package) is the first genuine backend: it reuses this codebase's own real,
already-verified model routing (``dourmouse.chat.ChatSession`` over an empty
``DispatchRegistry``, the exact same tool-less single-turn primitive
``goal_runtime.py``'s ``_verify_completion``/``_verify_goal_criteria`` already
use for independent reasoning passes) rather than inventing a new call path.
Study is real text extraction into a running context; answering is one real,
grounded model call instructed to cite only real studied filenames, which the
exam engine's own citation gate (exams.py) then independently verifies --
nothing about the anti-cheat design changes for a real backend.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class BrainNotConfigured(RuntimeError):
    """Raised when a Brain would need a backend that is not configured."""


@dataclass(frozen=True)
class BrainAnswer:
    text: str
    citations: tuple[str, ...] = ()


@dataclass
class Brain(ABC):
    """Abstract reasoning backend. Implementations must be deterministic-friendly."""

    configured: bool = False

    @abstractmethod
    def study(self, sources: list[Path], concepts: list[str]) -> dict[str, str]:
        """Ingest the given real source files + concept names.

        Returns a dict of learned facts keyed by source filename. The pipeline
        controls what is passed here, which is how held-out papers are kept out
        of the agent's knowledge.
        """

    @abstractmethod
    def answer(self, question: str) -> BrainAnswer:
        """Answer one exam question. Citations must name real corpus files."""

    @abstractmethod
    def remediate(self, feedback: str, sources: list[Path]) -> None:
        """Focused re-study after a failed attempt; may add to learned facts."""


def _extract_file_text(path: Path) -> str:
    """Plain text of a source: PDF text via pypdf when binary, else raw text."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw[:4] == b"%PDF":
        try:
            from pypdf import PdfReader

            r = PdfReader(str(path))
            return "\n".join((pg.extract_text() or "") for pg in r.pages)
        except Exception:
            return ""  # scanned/ocr-less: no extractable text
    return raw.decode("utf-8", errors="replace").strip()


class NotConfiguredBrain(Brain):
    """Honest stand-in until a real backend is configured."""

    configured: bool = False

    def study(self, sources: list[Path], concepts: list[str]) -> dict[str, str]:
        raise BrainNotConfigured(
            "no brain backend configured (set one by subclassing Brain); "
            "the agent cannot study for real yet"
        )

    def answer(self, question: str) -> BrainAnswer:
        raise BrainNotConfigured("no brain backend configured")

    def remediate(self, feedback: str, sources: list[Path]) -> None:
        raise BrainNotConfigured("no brain backend configured")


class MockBrain(Brain):
    """Deterministic test/dry-run brain.

    Knowledge = dict[filename -> learned text] populated by study(). Answers
    reproduce a studied file's content; unknown files yield an empty answer
    with no citations (which fails the citation gate), so held-out papers are
    unanswerable -- exactly the behavior a regurgitation-only model has.
    """

    configured: bool = True

    def __init__(self, defect_filenames: set[str] | None = None) -> None:
        self._facts: dict[str, str] = {}
        self._defects: set[str] = set(defect_filenames or set())

    def study(self, sources: list[Path], concepts: list[str]) -> dict[str, str]:
        learned: dict[str, str] = {}
        for src in sources:
            if not src.exists():
                continue
            text = _extract_file_text(src).strip()
            # Same-basename key files overwrite paper facts: a mock whose
            # answer is the key text passes the deterministic grader, which is
            # the behavior the pipeline tests need. Held-out keys are never
            # passed to study(), so this cannot leak a future exam's answers.
            fact = text[:1200] if text else "(empty source)"
            self._facts[src.name] = fact
            learned[src.name] = fact
        for concept in concepts:
            self._facts.setdefault(concept, f"studied concept: {concept}")
        return learned

    def answer(self, question: str) -> BrainAnswer:
        for name in self._facts:
            if name in question:
                if name in self._defects:
                    return BrainAnswer(
                        text=f"WRONG answer for {name}",
                        citations=(name,),
                    )
                return BrainAnswer(
                    text=self._facts[name],
                    citations=(name,),
                )
        return BrainAnswer(text="", citations=())

    def remediate(self, feedback: str, sources: list[Path]) -> None:
        # Remediation re-ingests the failed source and marks it no longer defective.
        for src in sources:
            if src.exists():
                text = _extract_file_text(src).strip()
                self._facts[src.name] = text[:1200] if text else "(empty source)"
        self._defects.clear()


# Total studied-context sent in one real answer() call. A real corpus can run
# to many megabytes of extracted PDF text; this keeps one prompt bounded the
# same way general_roster.py's _DELEGATE_RESULT_CAP bounds a delegate result.
_STUDY_CONTEXT_CAP_CHARS = 24_000
_CITATIONS_LINE = re.compile(r"CITATIONS:\s*(.+)", re.I)


class RealBrain(Brain):
    """A genuine, model-backed field-agent.

    Knowledge is real extracted text from real studied sources, kept as a
    running context (capped, oldest-trimmed) rather than one giant unbounded
    prompt. Each answer() call is one real, independent ChatSession turn over
    an empty DispatchRegistry -- grounded ONLY in what was actually studied,
    instructed to cite real filenames verbatim, never given tools (so this
    call can only reason, never act, mirroring goal_runtime.py's own
    independent-verification calls). The exam engine's citation gate then
    independently checks every claimed citation against the real corpus --
    RealBrain cannot pass an exam by asserting citations that do not exist.
    """

    configured: bool = True

    def __init__(self) -> None:
        self._facts: dict[str, str] = {}
        self._concepts: list[str] = []

    def _studied_context(self) -> str:
        parts = [f"[concept] {c}" for c in self._concepts]
        parts += [f"[source: {name}]\n{text}" for name, text in self._facts.items()]
        context = "\n\n".join(parts)
        if len(context) > _STUDY_CONTEXT_CAP_CHARS:
            context = context[-_STUDY_CONTEXT_CAP_CHARS:]
            context = "[earlier studied material truncated]\n" + context
        return context

    def study(self, sources: list[Path], concepts: list[str]) -> dict[str, str]:
        learned: dict[str, str] = {}
        for src in sources:
            if not src.exists():
                continue
            text = _extract_file_text(src).strip()
            fact = text if text else "(empty or unreadable source)"
            self._facts[src.name] = fact
            learned[src.name] = fact
        for concept in concepts:
            if concept not in self._concepts:
                self._concepts.append(concept)
        return learned

    def answer(self, question: str) -> BrainAnswer:
        from dourmouse.chat import ChatSession
        from dourmouse.dispatch import DispatchRegistry

        studied = self._studied_context()
        prompt = (
            "You are a field-agent sitting a real qualifying exam. Answer using "
            "ONLY the material you have actually studied below -- never invent "
            "facts, and never cite a source you were not given.\n\n"
            f"STUDIED MATERIAL:\n{studied or '(nothing studied yet)'}\n\n"
            f"EXAM QUESTION:\n{question}\n\n"
            "If the studied material does not let you answer, say so honestly "
            "instead of guessing. End your answer with exactly one line, "
            "verbatim, listing every source filename you actually used (comma "
            "separated, exact filenames only, none if you used none): "
            "'CITATIONS: file1.pdf, file2.pdf'"
        )
        try:
            session = ChatSession(DispatchRegistry(), session_file=None)
            result = session.ask(prompt, force_plain_dispatch=True)
        except Exception as exc:  # noqa: BLE001 - an exam attempt must fail honestly, not crash the pipeline
            return BrainAnswer(text=f"(brain call failed: {type(exc).__name__}: {exc})", citations=())
        text = (result.get("final_text") or "").strip()
        match = _CITATIONS_LINE.search(text)
        citations: tuple[str, ...] = ()
        if match:
            citations = tuple(
                c.strip() for c in match.group(1).split(",") if c.strip() and c.strip().lower() != "none"
            )
        return BrainAnswer(text=text, citations=citations)

    def remediate(self, feedback: str, sources: list[Path]) -> None:
        # Same real re-ingestion MockBrain performs: remediation's whole point
        # is that the agent is now ALLOWED to study the source it got wrong --
        # the actual re-reasoning happens on the next real answer() call once
        # that source is back in context, not in a separate throwaway call.
        for src in sources:
            if src.exists():
                text = _extract_file_text(src).strip()
                self._facts[src.name] = text if text else "(empty or unreadable source)"
