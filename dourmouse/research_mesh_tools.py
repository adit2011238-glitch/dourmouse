"""The ``research_mesh`` subagent -- tools that run and inspect real
field-specialist qualification (``dourmouse/research_mesh/``).

Mirrors ``goal_tools.py``'s own shape: one dedicated module, one
``build_research_mesh_subagent()`` factory returning a real ``Subagent``,
imported and registered by ``general_roster.py`` rather than adding more
bulk to that already-oversized file.

2026-09-19 rebuild of the orphaned ``jarvis/research_mesh/agents`` package
(disconnected from the live product, no real reasoning backend, stale a
month, never reachable from chat). The state machine underneath was already
real and already tested; what changed is a real model-backed ``RealBrain``
(``dourmouse/research_mesh/brain.py``) and this real chat-reachable wiring.

Known, named limitation (not hidden): ``research_mesh_qualify`` blocks the
calling tool call for the real duration of the run -- there is no
background/goal-runtime integration yet, so a field with a large real corpus
(some have 100+ papers) can take a genuinely long time. Real, separate,
not-yet-done follow-on work, the same honest-gap shape as Domain D's
tier-promotion.
"""

from __future__ import annotations

import json
from typing import Any

from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.research_mesh.brain import BrainNotConfigured, RealBrain
from dourmouse.research_mesh.core import AgentRecord
from dourmouse.research_mesh.exams import pending_iterations
from dourmouse.research_mesh.pipeline import DEFAULT_DB, PAPERS_ROOT, QualificationPipeline
from dourmouse.research_mesh.store import AgentStore
from dourmouse.research_mesh.study import load_corpus

_MAX_RESULT_HISTORY = 20


def _all_fields() -> list[tuple[str, str]]:
    manifest = PAPERS_ROOT / "MANIFEST.json"
    if not manifest.exists():
        return []
    data = json.loads(manifest.read_text())
    return [(f["domain"], f["field"]) for f in data.get("fields", [])]


def _research_mesh_status(arguments: dict[str, Any]) -> str:
    domain = str(arguments.get("domain") or "").strip()
    field = str(arguments.get("field") or "").strip()
    store = AgentStore(DEFAULT_DB)

    if domain and field:
        corpus = load_corpus(PAPERS_ROOT, domain, field)
        if not corpus.papers and not corpus.landing_pages:
            return f"ERROR: no materialized corpus for {domain} / {field}."
        rec = store.load(domain, field)
        if rec is None:
            pending = len(pending_iterations(corpus, set()))
            return (
                f"{domain} / {field}: not yet started. "
                f"{len(corpus.papers)} real exam paper(s) materialized, "
                f"{pending} pending iteration(s)."
            )
        lines = [
            f"{domain} / {field}: {rec.status.name}",
            f"passed {len(rec.passed_iterations)}/{len(corpus.papers)} real iteration(s), "
            f"study_minutes_used={rec.study_minutes_used}",
        ]
        for a in rec.history[-_MAX_RESULT_HISTORY:]:
            lines.append(
                f"  [{'PASS' if a.passed else 'FAIL'}] {a.iteration_id} "
                f"score={a.score:.2f} citations_verified={a.citations_verified}"
            )
        return "\n".join(lines)

    # No specific field: a real summary, honest about how much of the real
    # corpus is materialized (not every named field has real papers yet).
    all_fields = _all_fields()
    materialized = sum(
        1 for d, f in all_fields if load_corpus(PAPERS_ROOT, d, f).papers
    )
    counts = store.count_by_status()
    lines = [
        f"research mesh: {len(all_fields)} real field(s) known, "
        f"{materialized} with a materialized exam corpus.",
        "agents touched so far: "
        + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "none yet"),
    ]
    return "\n".join(lines)


def _research_mesh_qualify(arguments: dict[str, Any]) -> str:
    domain = str(arguments.get("domain") or "").strip()
    field = str(arguments.get("field") or "").strip()
    if not domain or not field:
        return "ERROR: research_mesh_qualify requires both 'domain' and 'field'."
    max_steps = arguments.get("max_steps") or 200
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        return "ERROR: 'max_steps' must be an integer."

    corpus = load_corpus(PAPERS_ROOT, domain, field)
    if not corpus.papers and not corpus.landing_pages:
        return (
            f"ERROR: no materialized corpus for {domain} / {field}. "
            "Call research_mesh_status with no arguments to see which fields have real papers."
        )

    store = AgentStore(DEFAULT_DB)
    record = store.load(domain, field) or AgentRecord(domain=domain, field=field)
    if record.status.terminal:
        return (
            f"{domain} / {field} is already {record.status.name} -- nothing to do. "
            f"Its real history has {len(record.history)} attempt(s)."
        )

    pipe = QualificationPipeline(store, RealBrain(), corpus)
    try:
        final = pipe.run(record, max_steps=max_steps)
    except BrainNotConfigured as exc:
        return f"ERROR: {exc}"
    except RuntimeError as exc:
        return f"INCOMPLETE: {exc}"

    lines = [
        f"{domain} / {field}: {final.name}",
        f"passed {len(record.passed_iterations)}/{len(corpus.papers)} real iteration(s), "
        f"study_minutes_used={record.study_minutes_used}",
    ]
    for a in record.history[-_MAX_RESULT_HISTORY:]:
        lines.append(
            f"  [{'PASS' if a.passed else 'FAIL'}] {a.iteration_id} "
            f"score={a.score:.2f} feedback={a.feedback}"
        )
    return "\n".join(lines)


def build_research_mesh_subagent() -> Subagent:
    return Subagent(
        name="research_mesh",
        domain="General",
        description=(
            "Real field-specialist qualification: an agent studies a real, "
            "held-out academic exam corpus for one field, sits a real exam, "
            "and only becomes QUALIFIED after genuinely passing -- never a "
            "self-reported claim, citation-gated grading against real "
            "corpus files. Use research_mesh_status to see what fields "
            "exist and their real state; research_mesh_qualify to run or "
            "resume one for real."
        ),
        tools=(
            ToolSpec(
                name="research_mesh_status",
                description=(
                    "Real status of one field-agent (pass domain and field), "
                    "or a real summary across the whole mesh (omit both)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string", "default": "",
                            "description": "Exact domain name, e.g. 'Condensed Matter & Materials (physics)'.",
                        },
                        "field": {
                            "type": "string", "default": "",
                            "description": "Exact field name within that domain.",
                        },
                    },
                },
                handler=_research_mesh_status,
            ),
            ToolSpec(
                name="research_mesh_qualify",
                description=(
                    "Run or resume one field-agent's real qualification: real "
                    "study of held-out sources, a real exam, real citation-"
                    "gated grading, real remediation on failure. Blocks for "
                    "the real duration of the run (can be minutes for a "
                    "field with many papers) -- no background/goal-runtime "
                    "integration yet, so avoid this for a field known to "
                    "have a large corpus without raising max_steps deliberately."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Exact domain name."},
                        "field": {"type": "string", "description": "Exact field name within that domain."},
                        "max_steps": {
                            "type": "integer", "default": 200,
                            "description": "Safety cap on pipeline steps this call will run before returning INCOMPLETE.",
                        },
                    },
                    "required": ["domain", "field"],
                },
                handler=_research_mesh_qualify,
            ),
        ),
    )
