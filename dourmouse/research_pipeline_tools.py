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

from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec
from dourmouse.research_pipeline.core import ResearchRecord
from dourmouse.research_pipeline.stages import (
    detect_contradictions,
    discover_sources,
    extract_evidence,
    plan,
    run_backward_edge,
    run_full_pipeline,
    synthesize,
)
from dourmouse.research_pipeline.store import ResearchStore, default_db


def _store() -> ResearchStore:
    return ResearchStore(default_db())


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


def _build_follow_up_tool(registry: DispatchRegistry) -> ToolSpec:
    def handler(arguments: dict[str, Any]) -> str:
        question = (arguments.get("question") or "").strip()
        err = _require_question(arguments, "research_follow_up")
        if err:
            return err
        record = _store().load(question)
        if record is None:
            return f"No real research record exists yet for {question!r}."
        before_claims, before_syn = len(record.claims), len(record.synthesis_history)
        try:
            run_backward_edge(record, registry)
        except ValueError as exc:
            return f"ERROR: {exc}"
        _save(record)
        done = [t for t in record.tasks if t.status == "DONE"]
        if len(record.synthesis_history) == before_syn and not record.open_tasks():
            return "Nothing to follow up: no unsettled contradiction on this record."
        lines = [
            f"Follow-up tasks worked: {len(done)} done, {len(record.open_tasks())} still open.",
            f"Claims: {before_claims} -> {len(record.claims)}.",
        ]
        if len(record.synthesis_history) > before_syn:
            lines.append(f"Revised synthesis (version {len(record.synthesis_history)}):\n{record.synthesis}")
        return "\n".join(lines)

    return ToolSpec(
        name="research_follow_up",
        description=(
            "The backward edge: for every contradiction on a synthesized "
            "research record, spawn a follow-up task, look for sources that "
            "settle it, extract that evidence, then re-check contradictions "
            "and write a REVISED synthesis (the earlier one is kept). Call "
            "after research_synthesize when contradictions were found."
        ),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string"}},
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
        f"follow-up tasks: {len(record.tasks)} ({len(record.open_tasks())} open)",
        f"synthesis versions: {len(record.synthesis_history)}",
    ]
    if record.synthesis:
        lines.append(f"synthesis: {record.synthesis}")
    return "\n".join(lines)


def _research_experiment_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import experiments as ex

    try:
        r = ex.run_experiment(str(arguments.get("protocol") or ""), str(arguments.get("code") or ""),
                              hypothesis_id=(arguments.get("hypothesis_id") or None),
                              wait_s=float(arguments.get("wait_s") or 120))
    except (ValueError, KeyError) as exc:
        return f"ERROR: {exc}"
    return f"experiment {r['experiment_id']}\n" + ex.describe_run(r["run_id"])


def _research_replicate_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import experiments as ex

    try:
        r = ex.replicate(str(arguments.get("run_id") or ""))
    except (ValueError, KeyError) as exc:
        return f"ERROR: {exc}"
    return f"replication {r['run_id']}: {r['verdict']}"


def _research_experiment_status_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import experiments as ex

    run_id = str(arguments.get("run_id") or "")
    try:
        ex.refresh_run(run_id)
        return ex.describe_run(run_id)
    except (ValueError, KeyError) as exc:
        return f"ERROR: {exc}"


def _research_hypothesize_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import hypotheses as hy

    try:
        r = hy.generate_hypotheses(str(arguments.get("question_id") or ""))
    except KeyError as exc:
        return f"ERROR: {exc}"
    if not r["ok"]:
        return "Not done: " + r["error"]
    lines = [f"{h['id']}: {h['statement']} (rests on {', '.join(h['claims'])})" for h in r["hypotheses"]]
    return "\n".join(lines + ([f"({r['dropped']} proposal(s) dropped: they cited no real claim.)"] if r["dropped"] else []))


def _research_critique_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import hypotheses as hy

    try:
        r = hy.criticize(str(arguments.get("hypothesis_id") or ""))
    except KeyError as exc:
        return f"ERROR: {exc}"
    if not r["ok"]:
        return "Not done: " + r["error"]
    rv = r["review"]
    return (f"Verdict: {r['verdict']}\nWeakest assumption: {rv['weakest_assumption']}\n"
            f"Alternative: {rv.get('alternative', '')}\nRefuted if: {rv['refuted_if']}")


def _research_critique_answer_tool(arguments: dict[str, Any]) -> str:
    """R9: read-only. The critic's own model call (if any) only decides partly
    matching sentences and every quote it gives is verified by the platform."""
    from dourmouse.research_graph.store import GraphStore
    from dourmouse.research_graph.store import default_db as graph_db
    from dourmouse.research_pipeline.answer_critic import critique_answer
    from dourmouse.research_pipeline.hypotheses import default_complete

    question_id = str(arguments.get("question_id") or "").strip()
    if not question_id:
        return "ERROR: research_critique_answer requires a non-empty 'question_id'."
    answer = str(arguments.get("answer") or "").strip()
    graph = GraphStore(graph_db())
    target: Any = question_id
    try:
        graph.get("research_question", question_id)
    except KeyError:
        # Not a graph id: the pipeline's other tools are keyed by the question text.
        record = _store().load(question_id)
        if record is None:
            return f"ERROR: no research question or record {question_id!r}."
        target = record
        answer = answer or record.synthesis.strip()
    else:
        if not answer:
            results = [e for e in graph.edges(dst=("research_question", question_id), relation="answers")
                       if e.src_type == "result"]
            if results:
                answer = str(graph.get("result", results[-1].src_id).body.get("summary") or "").strip()
    if not answer:
        return "Not done: this question has no synthesized answer yet, and none was given to check."
    supplied = bool(str(arguments.get("answer") or "").strip())
    result = critique_answer(answer, target, default_complete(), store=graph,
                             # The pipeline's own synthesis is written to cite each claim by URL.
                             require_citations=None if supplied else True)
    lines = [result.summary]
    for s in result.sentences:
        if s.verdict in ("SUPPORTED", "NOT_A_CLAIM"):
            continue
        ids = f" [{', '.join(s.evidence_ids)}]" if s.evidence_ids else ""
        lines.append(f"- {s.verdict}{ids}: {s.text} ({s.reason})")
    return "\n".join(lines)


def _research_design_experiment_tool(arguments: dict[str, Any]) -> str:
    from dourmouse.research_pipeline import hypotheses as hy

    try:
        r = hy.design_and_run(str(arguments.get("hypothesis_id") or ""))
    except KeyError as exc:
        return f"ERROR: {exc}"
    if not r["ok"]:
        return "Not done: " + r["error"]
    out = f"experiment {r['experiment_id']}, run {r['run_id']}: {r['state']}"
    st = r.get("statistics") or {}
    if st and "error" not in st:
        out += (f"\nn={st['n']}, mean={st['mean']:.6g}, sd={st['sd']:.6g}, 95% CI [{st['ci95'][0]:.6g}, "
                f"{st['ci95'][1]:.6g}], p={st['p_two_sided']:.4g} ({st['method']})")
    elif st:
        out += "\nstatistics: " + st["error"]
    return out


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
            _build_follow_up_tool(registry),
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
            # Finding #127 (R5 + RES-18): experiments as research objects.
            ToolSpec(
                name="research_experiment",
                description=(
                    "Test a hypothesis with an experiment: its protocol and Python code are recorded in the "
                    "research graph and executed as a sandboxed job on this Mac; the result, metrics "
                    "(from out/metrics.json), logs and environment hash are recorded and linked."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "protocol": {"type": "string", "description": "what is tested, and how"},
                        "code": {"type": "string", "description": "Python; write key numbers to out/metrics.json"},
                        "hypothesis_id": {"type": "string", "description": "optional: the hypothesis it tests"},
                        "wait_s": {"type": "number", "default": 120},
                    },
                    "required": ["protocol", "code"],
                },
                handler=_research_experiment_tool,
            ),
            ToolSpec(
                name="research_replicate",
                description="Replicate an experiment run and report whether every metric came out the same.",
                parameters={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
                handler=_research_replicate_tool,
            ),
            ToolSpec(
                name="research_experiment_status",
                description="Status, metrics, result and logs of one experiment run (brought up to date first).",
                parameters={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
                handler=_research_experiment_status_tool,
            ),
            # Finding #131 (R4): hypotheses, criticism, designed experiments.
            ToolSpec(
                name="research_hypothesize",
                description=("Propose testable hypotheses from a research question's sourced claims; each "
                             "must rest on real claims or it is not kept."),
                parameters={"type": "object", "properties": {"question_id": {"type": "string"}},
                            "required": ["question_id"]},
                handler=_research_hypothesize_tool,
            ),
            ToolSpec(
                name="research_critique",
                description="Have a critic review a hypothesis: weakest assumption, an alternative, what would refute it.",
                parameters={"type": "object", "properties": {"hypothesis_id": {"type": "string"}},
                            "required": ["hypothesis_id"]},
                handler=_research_critique_tool,
            ),
            ToolSpec(
                name="research_design_experiment",
                description=("Design an experiment for a hypothesis, run it on this Mac, and compute its "
                             "statistics (mean, confidence interval, p-value) without a model."),
                parameters={"type": "object", "properties": {"hypothesis_id": {"type": "string"}},
                            "required": ["hypothesis_id"]},
                handler=_research_design_experiment_tool,
            ),
            ToolSpec(
                name="research_critique_answer",
                description=("Critic pass over a synthesized research answer: flags each sentence that no stored "
                             "claim or source passage backs and each backed sentence lacking a citation, "
                             "verifying support deterministically. Reports only, changes nothing."),
                parameters={"type": "object",
                            "properties": {"question_id": {"type": "string"},
                                           "answer": {"type": "string",
                                                      "description": "optional: the answer text to check; "
                                                                     "default is the question's synthesized answer"}},
                            "required": ["question_id"]},
                handler=_research_critique_answer_tool,
                permission=Permission.REGULAR,
            ),
        ),
    )
