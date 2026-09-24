"""Real stage functions for the structured research pipeline (Domain G),
built on the data model in core.py (finding #042). Each stage is one real
call through this codebase's own already-proven machinery -- no new call
path invented:

- plan(): the same tool-less ChatSession(DispatchRegistry(), session_
  file=None) primitive used by _verify_completion/_verify_goal_criteria/
  RealBrain.answer -- this reasoning does not need tools, only the model's
  own decomposition of a question into real sub-questions.
- discover_sources(): a real run_dispatch_messages call forced to the
  already-real research_info subagent (the same forced_agent mechanism
  delegate_task itself uses) -- no new fetch/search code, reuses the
  live web_search/fetch_url tools exactly as they already work.
- extract_evidence(): a real forced fetch of one real source (same
  forced_agent mechanism again) followed by a real tool-less ChatSession
  call that must quote its supporting passage verbatim -- validated as a
  real substring of the real fetched text, never trusted blindly, so
  harsh acceptance test 1 (every claim clicks through to a real original
  passage) is enforced in code, not just hoped for in a prompt.
- synthesize(): the sixth reuse of the tool-less ChatSession primitive,
  given only the record's own real active Claims (never the raw fetched
  text) so the answer cannot smuggle in anything no Claim supports. A
  zero-active-claims record (every claim rejected, or none ever added)
  is handled without a model call at all -- there is nothing real to
  synthesize, and a model asked to summarize an empty claim list is
  exactly the kind of prompt that invites confident fabrication.
- detect_contradictions(): the seventh reuse. Groups active claims by the
  real sub-question they answered (a real Claim.sub_question field,
  added the same day this stage was built -- see the standing status
  report), then one real ChatSession call per pair within a group judges
  whether the two genuinely disagree, never an emergent hope that
  synthesis will notice on its own (harsh acceptance test 2).

Hypothesis generation and a dedicated criticism/revision pass are real,
separate, not-yet-built follow-on -- named explicitly, not silently
skipped, matching every other incremental piece this session. The state
machine in core.py has no separate stage for either (finding #042's own
docstring already says so); they are not silently folded into
SYNTHESIZED either.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from itertools import combinations
from typing import Any

from .core import Claim, Contradiction, ResearchRecord, Stage, Task, contradiction_key

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_CLAIM_RE = re.compile(r"CLAIM:\s*(.*?)\s*PASSAGE:\s*(.*?)\s*LOCATION:\s*(.*)", re.DOTALL)
# Live-caught (2026-09-20): force_plain_dispatch=True still runs through
# dispatch.py's own plan-reminder loop, which can misread part of a long,
# multi-instruction prompt as a declared multi-step "plan" -- with zero
# tools registered (this primitive is tool-less by design), no step can
# ever be satisfied, so the loop appends an honest "[DOURMOUSE: plan
# step(s) not executed via tools ...]" caveat to final_text. That caveat
# is real and correct for a live chat UI's own event feed (webui.py's
# incomplete_plan notice), never for a stored data field -- stripped here
# rather than fixed in dispatch.py's own plan-reminder loop, to avoid
# touching that shared, considerably more delicate machinery for a
# symptom only observed from research_pipeline's own long prompts.
_DIAGNOSTIC_SUFFIX_RE = re.compile(r"\n\n\[DOURMOUSE:.*\]\s*\Z", re.DOTALL)


def _strip_internal_diagnostics(text: str) -> str:
    return _DIAGNOSTIC_SUFFIX_RE.sub("", text).rstrip()

_PLAN_PROMPT = (
    "You are decomposing a research question into real, concrete sub-questions "
    "a researcher would actually need to answer before writing a real, sourced "
    "answer to the main question. Not vague restatements -- each sub-question "
    "should point at a genuinely different piece of evidence to go find.\n\n"
    "QUESTION: {question}\n\n"
    "List 2 to 5 real sub-questions, one per line, each starting with a dash "
    "'- '. Nothing else -- no preamble, no numbering, no closing remarks."
)

_DISCOVERY_INSTRUCTIONS = (
    "Research this question for real: search the live web and fetch at least "
    "one promising real page in full. Do not answer from memory -- use the "
    "real tools you have. Report back which real sources you actually found "
    "useful and why, in plain text.\n\nQUESTION: {question}"
)

_SYNTHESIS_PROMPT = (
    "You are writing the final, real, sourced answer to a research question, "
    "using ONLY the real claims listed below -- never state anything these "
    "claims do not support, and never fill a gap from outside knowledge.\n\n"
    "QUESTION: {question}\n\n"
    "REAL CLAIMS (each with its real source):\n{claims_block}\n\n"
    "{disagreements_block}"
    "Write a real, cohesive answer. Cite each claim by its real source URL "
    "inline. If the claims do not fully answer the question, say so honestly "
    "rather than guessing at the rest."
)

_DISAGREEMENTS = (
    "KNOWN DISAGREEMENTS between the claims (state each one plainly in the "
    "answer and say which side the evidence favours, or that it is unsettled; "
    "never quietly pick one side):\n{items}\n\n"
)

_FOLLOW_UP_DISCOVERY = (
    "{sub_question}\n\nTwo sources disagree on this: {note}. Find sources "
    "that settle it (primary sources, specifications, official documentation "
    "or data), not more of the same claims."
)

_NO_EVIDENCE_SYNTHESIS = (
    "No claims currently survive review for this question -- evidence "
    "extraction has not yet produced a supported answer."
)

_CONTRADICTION_PROMPT = (
    "Two real claims below both attempt to answer the same real research "
    "sub-question. Judge whether they genuinely CONTRADICT each other -- "
    "state incompatible facts -- as opposed to being merely different, "
    "complementary, or partial information that could both be true at "
    "once.\n\n"
    "SUB-QUESTION: {sub_question}\n\n"
    "CLAIM A: {claim_a}\n\n"
    "CLAIM B: {claim_b}\n\n"
    "Reply with exactly two lines, nothing else:\n"
    "CONTRADICTION: yes or no\n"
    "NOTE: one short real sentence explaining the judgment"
)

_CONTRADICTION_VERDICT_RE = re.compile(r"CONTRADICTION:\s*(yes|no)\b", re.IGNORECASE)

_EXTRACT_PROMPT = (
    "You are extracting one real, sourced claim from a real fetched web page to "
    "help answer a research sub-question. Use ONLY the fetched text below -- no "
    "outside knowledge, no guessing.\n\n"
    "SUB-QUESTION: {sub_question}\n\n"
    "FETCHED TEXT FROM {url}:\n{text}\n\n"
    "Reply with exactly three lines, nothing else:\n"
    "CLAIM: one real, specific sentence answering or informing the sub-question, "
    "based only on the fetched text above\n"
    "PASSAGE: the exact substring from the fetched text above that supports this "
    "claim, copied character-for-character -- never paraphrased\n"
    "LOCATION: a short real description of where this passage appears (e.g. "
    "'first paragraph', 'under the Architecture heading')"
)


def plan(record: ResearchRecord) -> ResearchRecord:
    """Real, tool-less ChatSession call producing real sub-questions.
    Mutates and returns `record`. Raises the underlying exception on a
    genuinely broken model call -- unlike RealBrain's own answer() (which
    must stay honest-uncertain mid-exam so a broken checker never destroys
    real completed work), a broken PLAN call has produced no real work yet
    to protect, so failing loudly here is the more honest choice."""
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    session = ChatSession(DispatchRegistry(), session_file=None)
    result = session.ask(
        _PLAN_PROMPT.format(question=record.question), force_plain_dispatch=True
    )
    text = _strip_internal_diagnostics((result.get("final_text") or "").strip())
    sub_questions = [
        line[2:].strip() for line in text.splitlines()
        if line.strip().startswith("- ") and line[2:].strip()
    ]
    if not sub_questions:
        # Honest fallback: the model answered but not in the requested
        # shape -- treat the whole real answer as one real sub-question
        # rather than fabricating a plan structure that was never there.
        sub_questions = [text] if text else [record.question]
    record.set_plan(sub_questions)
    return record


def _extract_urls_from_transcript(transcript: list[dict[str, Any]]) -> list[str]:
    """Real URLs from a real dispatch transcript -- two sources, most
    reliable first: fetch_url's own tool_call arguments (a clean, exact
    URL, never regex-extracted from prose), then a best-effort regex scan
    of web_search's own formatted result text (which embeds a real URL per
    hit for the Brave/DuckDuckGo engines, but NOT for the Wikipedia
    fallback -- an honest, named gap in that one engine's own output
    shape, not something this function can recover)."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for entry in transcript:
        if entry.get("type") == "tool_use" and entry.get("name") == "fetch_url":
            try:
                args = json.loads(entry.get("raw_arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            url = str(args.get("url") or "").strip()
            if url:
                _add(url)
        elif entry.get("type") == "tool_result" and entry.get("name") == "web_search":
            for m in _URL_RE.finditer(entry.get("text") or ""):
                _add(m.group(0))
    return urls


def discover_sources(
    record: ResearchRecord,
    registry: Any,
    *,
    sub_question_index: int = 0,
    query: str | None = None,
    **dispatch_kwargs: Any,
) -> ResearchRecord:
    """Real source discovery: a real nested dispatch run forced to the
    already-real research_info subagent, reading back the real URLs it
    actually touched. `sub_question_index` picks which real sub-question
    from the plan drives this call -- one call per sub-question is the
    real, separate follow-on loop a caller builds on top of this
    function; this function itself does exactly one real discovery pass.
    """
    from dourmouse.dispatch import run_dispatch_messages

    if not record.plan:
        raise ValueError("cannot discover sources before a real plan exists")
    sub_question = query or record.plan[sub_question_index]
    messages = [
        {"role": "user", "content": _DISCOVERY_INSTRUCTIONS.format(question=sub_question)},
    ]
    report = run_dispatch_messages(
        messages, registry, forced_agent="research_info", **dispatch_kwargs
    )
    urls = _extract_urls_from_transcript(report.get("transcript") or [])
    record.add_sources(urls)
    return record


#: Characters of the document shown to the extraction model (about 15k
#: tokens). Passage validation always uses the whole document.
_EXTRACT_PROMPT_CHARS = 60_000


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _source_id_for_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_evidence(
    record: ResearchRecord,
    registry: Any,
    *,
    source_index: int = 0,
    sub_question_index: int = 0,
    sub_question: str | None = None,
    task_id: str = "",
    **dispatch_kwargs: Any,
) -> ResearchRecord:
    """Real per-source evidence extraction. The source is fetched directly
    through the research acquisition layer (finding #089): SSRF-guarded,
    decoded with its real charset, the raw bytes stored content-addressed
    so the citation can be re-read later. Then one real tool-less
    ChatSession call (same primitive as plan()) is asked to quote its
    supporting passage verbatim. The quote is never trusted on the model's
    word alone -- it is checked as a real (whitespace-normalized) substring
    of the real fetched text before becoming a `Claim`, so harsh acceptance
    test 1 ("click through to the ORIGINAL passage") is a real, enforced
    guarantee, not just a prompt request. Raises loudly on a genuinely
    failed fetch or a claim whose passage cannot be verified -- same "no
    real work yet to protect" honesty as plan()'s own failure mode.

    Before #089 the fetch was a nested LLM dispatch told to call fetch_url
    on an already-known URL, and the claim was built from that tool's text
    output: capped at 8000 characters, UTF-8 regardless of charset, and
    cached as that stripped text, so ``document_hash`` fingerprinted text
    that matched no stored document. `registry` and `dispatch_kwargs` are
    kept for call-site compatibility and no longer used here."""
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    from .acquire import cut_at_word, fetch_document

    if not record.plan:
        raise ValueError("cannot extract evidence before a real plan exists")
    if not record.sources:
        raise ValueError("cannot extract evidence before real sources exist")
    url = record.sources[source_index]
    sub_question = sub_question or record.plan[sub_question_index]

    try:
        doc = fetch_document(url)
    except Exception as exc:  # noqa: BLE001 - re-raised with the source named
        raise ValueError(f"could not fetch real content from {url} for evidence extraction: {exc}") from exc
    fetched_text = doc.text
    if not fetched_text.strip():
        raise ValueError(f"could not fetch real content from {url} for evidence extraction: no readable text")
    # The model sees at most this much; passage validation below still runs
    # against the WHOLE document. Chunked extraction over long documents is
    # R1 work (passages as first-class objects).
    prompt_text, _cut = cut_at_word(fetched_text, _EXTRACT_PROMPT_CHARS)

    session = ChatSession(DispatchRegistry(), session_file=None)
    result = session.ask(
        _EXTRACT_PROMPT.format(sub_question=sub_question, url=url, text=prompt_text),
        force_plain_dispatch=True,
    )
    reply = _strip_internal_diagnostics((result.get("final_text") or "").strip())
    match = _CLAIM_RE.search(reply)
    if not match:
        raise ValueError(f"model did not return a real CLAIM/PASSAGE/LOCATION triple for {url}")
    claim_text, passage, location = (g.strip() for g in match.groups())
    if not claim_text or not passage:
        raise ValueError(f"model returned an empty claim or passage for {url}")
    if _normalize_whitespace(passage) not in _normalize_whitespace(fetched_text):
        raise ValueError(
            f"passage validation failed for {url}: the model's quoted passage is not a "
            "real substring of the real fetched text (rejected, not silently accepted)"
        )

    # The location comes from the document's own heading structure when it
    # has one (finding #091): the model's LOCATION line was a guess, the
    # heading path is a fact. The model's wording is kept only as fallback.
    computed = doc.structure.locate(passage) if doc.structure is not None else None
    if computed:
        location = computed

    claim = Claim(
        claim=claim_text,
        source_id=_source_id_for_url(doc.final_url),
        url=url,
        # The hash of the raw bytes actually stored (acquire.DocumentCache),
        # so the cited document can be re-read and re-verified by hash.
        document_hash=doc.raw_sha256,
        location=location,
        passage=passage,
        retrieved_at=time.time(),
        agent="research_info",
        sub_question=sub_question,
        final_url=doc.final_url,
        task_id=task_id,
    )
    record.add_claim(claim)
    return record


def synthesize(record: ResearchRecord) -> ResearchRecord:
    """Real final-answer synthesis, given only the record's own real
    active Claims -- never the raw fetched text, so the answer cannot
    smuggle in anything no Claim actually supports. Zero active claims
    (every claim rejected, or none ever added) skips the model call
    entirely: there is nothing real to synthesize, and asking a model to
    summarize an empty claim list only invites it to fabricate one. A
    genuinely empty model reply (real claims DO exist here, unlike
    plan()'s "nothing to protect yet" case) degrades to an honest
    placeholder instead of raising -- same "never destroy real completed
    work over a downstream hiccup" reasoning as RealBrain.answer's own
    honest-uncertain fallback."""
    active = record.active_claims()
    if not active:
        record.set_synthesis(_NO_EVIDENCE_SYNTHESIS)
        return record

    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    claims_block = "\n".join(
        f"{i}. {c.claim} (source: {c.url})" for i, c in enumerate(active, 1)
    )
    # Contradictions are surfaced, never silently resolved by omission
    # (harsh acceptance test 2; finding #096: synthesis used to ignore them).
    by_fp = {_claim_fingerprint(c): c for c in active}
    items = [
        f"- {by_fp[k.claim_a_id].claim} VERSUS {by_fp[k.claim_b_id].claim} ({k.note})"
        for k in record.contradictions
        if k.claim_a_id in by_fp and k.claim_b_id in by_fp
    ]
    disagreements = _DISAGREEMENTS.format(items="\n".join(items)) if items else ""
    session = ChatSession(DispatchRegistry(), session_file=None)
    result = session.ask(
        _SYNTHESIS_PROMPT.format(
            question=record.question, claims_block=claims_block, disagreements_block=disagreements,
        ),
        force_plain_dispatch=True,
    )
    text = _strip_internal_diagnostics((result.get("final_text") or "").strip())
    if not text:
        text = (
            f"Synthesis unavailable: the model returned no real text. "
            f"{len(active)} real claim(s) remain on record for direct review."
        )
    record.set_synthesis(text)
    return record


def _parse_contradiction_reply(reply: str) -> tuple[str, str] | None:
    """Live-caught (2026-09-20): a real model correctly judged a genuine,
    seeded contradiction as "yes" but never emitted the literal "NOTE:"
    label the prompt asked for -- it just continued straight into its own
    explanation. Requiring that exact label lost a real, correct verdict
    to a formatting miss, the same class of over-strict parsing this
    session has already fixed once for plan()'s own dash-line fallback.
    Only the verdict marker is required; everything after it becomes the
    note, with a leading "NOTE:" label stripped if the model DOES include
    it. Returns None only when no real verdict marker is found at all."""
    match = _CONTRADICTION_VERDICT_RE.search(reply)
    if not match:
        return None
    verdict = match.group(1).lower()
    note = re.sub(r"^NOTE:\s*", "", reply[match.end():].strip(), flags=re.IGNORECASE).strip()
    return verdict, note


def _claim_fingerprint(claim: Claim) -> str:
    """core.py's own Contradiction docstring names this exact shape:
    Claim.source_id + a real hash of the claim text -- stable across
    save/load (unlike a claim's own list index, which shifts if an
    earlier claim is ever inserted)."""
    return f"{claim.source_id}:{hashlib.sha256(claim.claim.encode('utf-8')).hexdigest()[:12]}"


def run_full_pipeline(
    record: ResearchRecord,
    registry: Any,
    *,
    max_sources_per_sub_question: int = 3,
    **dispatch_kwargs: Any,
) -> ResearchRecord:
    """The real multi-source/multi-sub-question orchestration loop -- the
    last core-loop gap named explicitly in finding #050. A caller-side
    loop over the exact same discover_sources()/extract_evidence() calls
    a human operator already drives one at a time through the chat tools
    -- no new dispatch machinery, no new prompt, nothing this domain has
    not already proven live.

    Walks every real sub-question in the plan in order, discovers real
    sources for it, then extracts evidence from up to
    `max_sources_per_sub_question` of the real, newly-found sources for
    THAT sub-question (tracked by list position before/after the
    discover_sources() call -- add_sources() already dedupes globally, so
    a source rediscovered for a later sub-question is correctly skipped
    here rather than double-extracted). The cap is a real, named cost
    bound: a live web search can return many hits, and extracting from
    every one of them uncapped is an uncontrolled real cost per
    sub-question, not a free iteration.

    A single source that fails extraction (a bad fetch, a hallucinated or
    unverifiable passage -- extract_evidence()'s own real ValueError
    cases) is skipped, not fatal to the rest of the run -- the same "one
    bad pairing must never block every other one still to check"
    reasoning detect_contradictions() already uses. A research record
    with real partial success from N-1 sources is more honest and more
    useful than aborting the whole pass over one source's failure.

    Requires a real plan already set -- raises loudly, matching plan()'s
    and extract_evidence()'s own "no real work yet to protect" honesty:
    an orchestration loop with no plan to walk is not a partial failure,
    it is a caller error."""
    if not record.plan:
        raise ValueError("cannot run the full pipeline before a real plan exists")
    for sub_question_index in range(len(record.plan)):
        before = len(record.sources)
        discover_sources(
            record, registry, sub_question_index=sub_question_index, **dispatch_kwargs
        )
        new_source_indices = list(range(before, len(record.sources)))[
            :max_sources_per_sub_question
        ]
        for source_index in new_source_indices:
            try:
                extract_evidence(
                    record, registry,
                    source_index=source_index, sub_question_index=sub_question_index,
                    **dispatch_kwargs,
                )
            except ValueError:
                continue  # one bad source must never block the rest of the real run
    return record


def detect_contradictions(record: ResearchRecord) -> ResearchRecord:
    """Real contradiction detection (harsh acceptance test 2): groups
    active claims by the real sub-question they answered, then one real
    tool-less ChatSession call per PAIR within a group judges whether the
    two genuinely disagree -- never an emergent hope that synthesis will
    notice on its own. A sub-question with fewer than two active claims
    has nothing to compare and costs no model call, same "don't invite
    fabrication over nothing" discipline as synthesize()'s own
    zero-active-claims case. A malformed model reply is skipped, not
    raised -- same reasoning as synthesize()'s own honest-degrade: real
    claims already exist here, and a single bad judgment on one pair must
    never block every other pair still to be checked."""
    groups: dict[str, list[Claim]] = {}
    for c in record.active_claims():
        if c.sub_question:
            groups.setdefault(c.sub_question, []).append(c)

    # Pairs already recorded as contradicting are not asked again: re-running
    # detection after a follow-up round must cost only the NEW pairs.
    known = {frozenset((k.claim_a_id, k.claim_b_id)) for k in record.contradictions}
    pairs_to_check = [
        (sub_question, a, b)
        for sub_question, claims in groups.items()
        if len(claims) >= 2
        for a, b in combinations(claims, 2)
        if frozenset((_claim_fingerprint(a), _claim_fingerprint(b))) not in known
    ]
    if not pairs_to_check:
        return record

    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    for sub_question, a, b in pairs_to_check:
        session = ChatSession(DispatchRegistry(), session_file=None)
        result = session.ask(
            _CONTRADICTION_PROMPT.format(
                sub_question=sub_question, claim_a=a.claim, claim_b=b.claim
            ),
            force_plain_dispatch=True,
        )
        reply = _strip_internal_diagnostics((result.get("final_text") or "").strip())
        parsed = _parse_contradiction_reply(reply)
        if parsed is None:
            continue  # no real verdict marker found -- skip this pair, never raise
        verdict, note = parsed
        if verdict == "yes":
            record.add_contradiction(
                Contradiction(
                    claim_a_id=_claim_fingerprint(a),
                    claim_b_id=_claim_fingerprint(b),
                    sub_question=sub_question,
                    note=note,
                )
            )
    return record


def spawn_follow_ups(record: ResearchRecord) -> tuple[Task, ...]:
    """The backward edge (R3, finding #096): every contradiction without
    follow-up work spawns a Task to settle it. Returns the tasks still open."""
    for k in record.contradictions:
        record.spawn_task_for(k)
    return record.open_tasks()


def run_follow_up(
    record: ResearchRecord,
    registry: Any,
    task_id: str,
    *,
    max_sources: int = 2,
    **dispatch_kwargs: Any,
) -> ResearchRecord:
    """Work one follow-up task: discover sources aimed at settling its
    contradiction, extract evidence tagged with the task, then close it.
    One bad source never blocks the rest (same rule as run_full_pipeline).
    The record's own stage does not move: the project only goes forward and
    the evidence grows."""
    task = next(t for t in record.tasks if t.task_id == task_id)
    if task.status != "OPEN":
        raise ValueError(f"task {task_id} is not open")
    contradiction = next(
        (k for k in record.contradictions if contradiction_key(k) == task.spawned_by), None,
    )
    note = contradiction.note if contradiction is not None else task.title
    before = len(record.sources)
    discover_sources(
        record, registry,
        query=_FOLLOW_UP_DISCOVERY.format(sub_question=task.sub_question or record.question, note=note),
        **dispatch_kwargs,
    )
    for source_index in list(range(before, len(record.sources)))[:max_sources]:
        try:
            extract_evidence(
                record, registry, source_index=source_index,
                sub_question=task.sub_question or record.question, task_id=task_id,
                **dispatch_kwargs,
            )
        except ValueError:
            continue
    record.complete_task(task_id)
    return record


def run_backward_edge(
    record: ResearchRecord, registry: Any, *, max_tasks: int = 3, **dispatch_kwargs: Any,
) -> ResearchRecord:
    """Contradiction -> new task -> new evidence -> revised synthesis, once.
    Bounded: at most ``max_tasks`` follow-ups per call, so a pile of
    contradictions cannot become an unbounded spend. Needs a synthesis to
    revise; a record not yet synthesized has nothing to go back from."""
    if record.stage is not Stage.SYNTHESIZED:
        raise ValueError("the backward edge starts from a synthesized record")
    ran = spawn_follow_ups(record)[:max_tasks]
    if not ran:
        return record  # nothing unsettled: re-synthesizing would only cost a call
    for task in ran:
        run_follow_up(record, registry, task.task_id, **dispatch_kwargs)
    if record.open_tasks():
        return record  # over the bound: the rest wait for the next call
    detect_contradictions(record)
    synthesize(record)
    return record
