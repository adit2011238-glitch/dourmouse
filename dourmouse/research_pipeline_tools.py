"""The ``evidence_pipeline`` subagent -- chat-reachable tools over Domain G's
structured research pipeline (``dourmouse/research_pipeline/``).

Mirrors ``research_mesh_tools.py``'s own shape: one dedicated module, one
``build_research_pipeline_subagent()`` factory returning a real ``Subagent``,
imported and registered by ``general_roster.py``. Unlike ``research_mesh_
tools.py``, this factory takes ``registry`` as a parameter -- matching
``general_roster.py``'s own ``_make_code_tool(backend, registry)``
convention -- because ``discover_sources()``/``extract_evidence()`` need a
live registry to force a nested dispatch onto the real ``research_info``
subagent.

Real gap this closes: every prior Domain G stage function (finding #042
through #049) was only reachable by writing a short Python script. Nothing
here changes the stage functions themselves -- this is pure chat-reachable
wiring over already-proven, already-tested logic, the same "no new call
path" discipline this domain has followed throughout.

Known, named limitation (not hidden): each tool call runs one real stage
SYNCHRONOUSLY and can take anywhere from a few seconds (``research_plan``)
to tens of seconds (a real web fetch plus a real model call, for
``research_discover_sources``/``research_extract_evidence``) -- there is no
background/goal-runtime integration yet, the same already-documented
limitation ``research_mesh_qualify`` carries. A record persists across
calls (``ResearchStore``, keyed by the exact question text), so a killed
conversation resumes exactly where it stopped, matching this domain's own
"auditable, resumable" design goal.

``research_run_pipeline`` (added after the real ``evidence_pipeline``
chat-reachability gap closed, finding #050) drives ``research_discover_
sources``/``research_extract_evidence`` across EVERY real sub-question in
one call -- the multi-source/multi-sub-question orchestration loop this
domain's own status report named as its last real core-loop gap. The
finer-grained per-sub-question/per-source tools remain, for a caller that
wants manual control over which source gets extracted.
"""

from __future__ import annotations

import time
from typing import Any

from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.research_pipeline.core import ResearchRecord
from dourmouse.research_pipeline.stages import (
    detect_contradictions,
    discover_sources,
    extract_evidence,
    plan,
    run_full_pipeline,
    synthesize,
)
from dourmouse.research_pipeline.store import DEFAULT_DB, ResearchStore


def _store() -> ResearchStore:
    return ResearchStore(DEFAULT_DB)


def _load_or_start(question: str) -> ResearchRecord:
    record = _store().load(question)
    return record if record is not None else ResearchRecord(question=question)


def _save(record: ResearchRecord) -> None:
    _store().save(record, now=time.time())


def _require_question(arguments: dict[str, Any], tool_name: str) -> str | None:
    question = (arguments.get("question") or "").strip()
    if not question:
        return f"ERROR: {tool_name} requires a non-empty 'question'."
    return None


def _format_plan(record: ResearchRecord) -> str:
    return "Real sub-questions:\n" + "\n".join(f"- {q}" for q in record.plan)


def _research_plan_tool(arguments: dict[str, Any]) -> str:
    question = (arguments.get("question") or "").strip()
    err = _require_question(arguments, "research_plan")
    if err:
        return err
    record = _load_or_start(question)
    if record.plan:
        return f"Already planned. {_format_plan(record)}"
    plan(record)
    _save(record)
    return _format_plan(record)


def _build_discover_sources_tool(registry: DispatchRegistry) -> ToolSpec:
    def handler(arguments: dict[str, Any]) -> str:
        question = (arguments.get("question") or "").strip()
        err = _require_question(arguments, "research_discover_sources")
        if err:
            return err
        record = _load_or_start(question)
        if not record.plan:
            return "ERROR: call research_plan first -- no real plan exists yet."
        try:
            sub_question_index = int(arguments.get("sub_question_index", 0))
        except (TypeError, ValueError):
            return "ERROR: sub_question_index must be an integer."
        try:
            discover_sources(record, registry, sub_question_index=sub_question_index)
        except (ValueError, IndexError) as exc:
            return f"ERROR: {exc}"
        _save(record)
        if not record.sources:
            return "Real discovery ran but found zero real sources for that sub-question."
        return "Real sources found:\n" + "\n".join(f"- {u}" for u in record.sources)

    return ToolSpec(
        name="research_discover_sources",
        description=(
            "Search the live web for real sources answering one sub-question "
            "from an existing research plan (call research_plan first). "
            "Persists the real URLs found onto the research record."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "sub_question_index": {"type": "integer", "default": 0},
            },
            "required": ["question"],
        },
        handler=handler,
    )


def _build_extract_evidence_tool(registry: DispatchRegistry) -> ToolSpec:
    def handler(arguments: dict[str, Any]) -> str:
        question = (arguments.get("question") or "").strip()
        err = _require_question(arguments, "research_extract_evidence")
        if err:
            return err
        record = _load_or_start(question)
        if not record.sources:
            return "ERROR: call research_discover_sources first -- no real sources exist yet."
        try:
            source_index = int(arguments.get("source_index", 0))
            sub_question_index = int(arguments.get("sub_question_index", 0))
        except (TypeError, ValueError):
            return "ERROR: source_index/sub_question_index must be integers."
        try:
            extract_evidence(
                record, registry,
                source_index=source_index, sub_question_index=sub_question_index,
            )
        except (ValueError, IndexError) as exc:
            return f"ERROR: {exc}"
        _save(record)
        c = record.claims[-1]
        return (
            f"Real claim extracted: {c.claim!r}\n"
            f"Source: {c.url}\nPassage: {c.passage!r}\nLocation: {c.location}"
        )

    return ToolSpec(
        name="research_extract_evidence",
        description=(
            "Fetch one real source (by index into the sources already found "
            "via research_discover_sources) and extract one real, quoted "
            "claim from it. The quote is verified against the real fetched "
            "text before being accepted -- a rejected/hallucinated quote "
            "reports an honest error, never a fabricated claim."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "source_index": {"type": "integer", "default": 0},
                "sub_question_index": {"type": "integer", "default": 0},
            },
            "required": ["question"],
        },
        handler=handler,
    )


def _build_run_pipeline_tool(registry: DispatchRegistry) -> ToolSpec:
    def handler(arguments: dict[str, Any]) -> str:
        question = (arguments.get("question") or "").strip()
        err = _require_question(arguments, "research_run_pipeline")
        if err:
            return err
        record = _load_or_start(question)
        if not record.plan:
            return "ERROR: call research_plan first -- no real plan exists yet."
        try:
            max_sources = int(arguments.get("max_sources_per_sub_question", 3))
        except (TypeError, ValueError):
            return "ERROR: max_sources_per_sub_question must be an integer."
        before_sources, before_claims = len(record.sources), len(record.claims)
        run_full_pipeline(record, registry, max_sources_per_sub_question=max_sources)
        _save(record)
        return (
            f"Real pipeline run complete over {len(record.plan)} sub-question(s). "
            f"Sources: {before_sources} -> {len(record.sources)}. "
            f"Claims: {before_claims} -> {len(record.claims)}."
        )

    return ToolSpec(
        name="research_run_pipeline",
        description=(
            "Run discovery and evidence extraction for EVERY sub-question in "
            "an existing real plan (call research_plan first) in one call -- "
            "the multi-source/multi-sub-question orchestration loop, rather "
            "than driving research_discover_sources/research_extract_evidence "
            "one sub-question and one source at a time. A source that fails "
            "extraction is skipped, not fatal to the rest of the run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "max_sources_per_sub_question": {"type": "integer", "default": 3},
            },
            "required": ["question"],
        },
        handler=handler,
    )


def _research_detect_contradictions_tool(arguments: dict[str, Any]) -> str:
    question = (arguments.get("question") or "").strip()
    err = _require_question(arguments, "research_detect_contradictions")
    if err:
        return err
    record = _load_or_start(question)
    before = len(record.contradictions)
    detect_contradictions(record)
    _save(record)
    new = record.contradictions[before:]
    if not new:
        return "No real contradictions found among the current active claims."
    lines = [f"{len(new)} real contradiction(s) found:"]
    for c in new:
        lines.append(f"- ({c.sub_question}) {c.note}")
    return "\n".join(lines)


def _research_synthesize_tool(arguments: dict[str, Any]) -> str:
    question = (arguments.get("question") or "").strip()
    err = _require_question(arguments, "research_synthesize")
    if err:
        return err
    record = _load_or_start(question)
    try:
        synthesize(record)
    except ValueError as exc:
        return f"ERROR: {exc}"
    _save(record)
    return record.synthesis


def _research_status_tool(arguments: dict[str, Any]) -> str:
    question = (arguments.get("question") or "").strip()
    err = _require_question(arguments, "research_status")
    if err:
        return err
    record = _store().load(question)
    if record is None:
        return f"No real research record exists yet for {question!r}."
    active = record.active_claims()
    lines = [
        f"question: {record.question}",
        f"stage: {record.stage.name}",
        f"plan: {len(record.plan)} real sub-question(s)",
        f"sources: {len(record.sources)} real URL(s)",
        f"claims: {len(record.claims)} total, {len(active)} active",
        f"contradictions: {len(record.contradictions)}",
    ]
    if record.synthesis:
        lines.append(f"synthesis: {record.synthesis}")
    return "\n".join(lines)


def build_research_pipeline_subagent(registry: DispatchRegistry) -> Subagent:
    return Subagent(
        name="evidence_pipeline",
        domain="General",
        description=(
            "Structured, evidence-based research (Domain G): plan real "
            "sub-questions, discover real web sources, extract real quoted "
            "claims, detect real contradictions, and synthesize a real, "
            "citation-only answer. Each call runs one real stage; call "
            "these tools in sequence to drive a full research pass."
        ),
        tools=(
            ToolSpec(
                name="research_plan",
                description=(
                    "Decompose a real research question into real sub-"
                    "questions. Always the first real step for a new "
                    "question; safe to call again, it reports the "
                    "already-real plan rather than re-planning."
                ),
                parameters={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
                handler=_research_plan_tool,
            ),
            _build_discover_sources_tool(registry),
            _build_extract_evidence_tool(registry),
            _build_run_pipeline_tool(registry),
            ToolSpec(
                name="research_detect_contradictions",
                description=(
                    "Compare every pair of active claims that answer the "
                    "same real sub-question and record any genuine "
                    "contradiction found. Call after extracting evidence "
                    "from 2+ sources for the same sub-question."
                ),
                parameters={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
                handler=_research_detect_contradictions_tool,
            ),
            ToolSpec(
                name="research_synthesize",
                description=(
                    "Write the final, real, citation-only answer from every "
                    "surviving active claim on the record. Call last, once "
                    "enough evidence has been extracted."
                ),
                parameters={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
                handler=_research_synthesize_tool,
            ),
            ToolSpec(
                name="research_status",
                description=(
                    "Report the real current stage, plan, sources, claims, "
                    "contradictions, and synthesis (if any) for a research "
                    "question already in progress."
                ),
                parameters={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
                handler=_research_status_tool,
            ),
        ),
    )
