"""Brain interface: the seam between the pipeline and whatever reasons for the agent.

Design (why this exists):

- The qualification pipeline must be testable and runnable *without* an LLM.
  Every reasoning capability — studying sources, answering exam questions,
  remediating after feedback — goes through this interface, so tests inject a
  deterministic MockBrain and the pilot can dry-run the whole loop offline.
- A real backend (an LLM API, a local model, Dourmouse's own runtime) plugs in
  by implementing `Brain`; nothing else in the pipeline changes. That keeps
  the pipeline decoupled from any vendor, exactly like the project's
  fetch/clock injection conventions.
- `BrainNotConfigured` is raised (never silently swallowed) when no backend is
  configured, so the system is honest about what it cannot do — the same
  fail-closed discipline as the rest of the codebase.

The MockBrain models an agent whose knowledge is *exactly* the sources it was
given to study: it answers from learned facts keyed by source filename. This
makes the anti-cheat rule directly testable — a mock that studied only
non-held-out papers cannot answer a held-out exam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    unanswerable — exactly the behavior a regurgitation-only model has.
    """

    configured: bool = True

    def __init__(self, defect_filenames: set[str] | None = None) -> None:
        self._facts: dict[str, str] = {}
        self._defects: set[str] = set(defect_filenames or set())

    @staticmethod
    def _file_text(path: Path) -> str:
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

    def study(self, sources: list[Path], concepts: list[str]) -> dict[str, str]:
        learned: dict[str, str] = {}
        for src in sources:
            if not src.exists():
                continue
            text = self._file_text(src).strip()
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
                text = self._file_text(src).strip()
                self._facts[src.name] = text[:1200] if text else "(empty source)"
        self._defects.clear()
