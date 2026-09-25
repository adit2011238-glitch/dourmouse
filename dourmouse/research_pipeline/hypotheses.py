"""The remaining research stages (R4, finding #131): hypothesis generation
(RES-13), hypothesis criticism (RES-14), experiment design (RES-15) and
statistical analysis (RES-17).

Everything is stored in the research graph, linked to what it came from:

    hypothesis --derived_from--> claim        (the evidence it grew out of)
    hypothesis --about--> research_question
    hypothesis --revised_by--> decision       (a criticism, versioned)
    hypothesis --tested_by--> experiment      (a designed and run experiment)
    result (statistics) --about--> experiment_run

The model proposes; nothing it says is taken on trust. A hypothesis must
cite claims that exist, a criticism is recorded as a decision the owner can
read, a designed experiment runs through the same sandboxed job runner, and
the statistics are computed deterministically (no model) from the run's
own data.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Callable
from typing import Any

from dourmouse.research_graph.store import GraphStore
from dourmouse.research_graph.store import default_db as _graph_db

Complete = Callable[[str], str]
BY = "hypotheses"

_GENERATE = (
    "You are generating research hypotheses. Use ONLY the numbered claims below (each is sourced). "
    "Propose 1 to 3 testable hypotheses that would explain or extend them. Every hypothesis must cite the "
    "claim numbers it rests on. Answer with ONLY JSON: "
    '{{"hypotheses": [{{"statement": "...", "rationale": "...", "claims": [1, 2]}}]}}\n\n'
    "QUESTION: {question}\n\nCLAIMS:\n{claims}"
)
_CRITICIZE = (
    "You are a critical reviewer of a research hypothesis. Answer with ONLY a JSON object with four keys: "
    "weakest_assumption (the assumption most likely to be wrong, in your own words), alternative (a "
    "different explanation of the same observations), refuted_if (a concrete result that would refute "
    "it), and verdict (exactly one of the words plausible, weak, untestable). Be specific to THIS "
    "hypothesis.\n\nHYPOTHESIS: {statement}\nRATIONALE: {rationale}"
)
_DESIGN = (
    "Design a small, runnable Python experiment that tests this hypothesis with simulation or computation "
    "(no network access, standard library plus numpy only). The code must write its raw observations to "
    "out/data.json as {{\"samples\": [numbers]}} and its key numbers to out/metrics.json. Answer with ONLY a "
    "JSON object with three keys: protocol (what is tested and how), code (the Python source), and "
    "null_value (the number the samples' mean would be if the hypothesis were FALSE, or the value it "
    "predicts; the statistics test the mean against it).\n\nHYPOTHESIS: {statement}"
)
_PLACEHOLDER = re.compile(r"^\s*(\.\.\.|…|<[^>]*>|plausible\|weak\|untestable)\s*$")


def default_complete() -> Complete:
    """The research pipeline's own model call (cloud, tool-less)."""
    def complete(prompt: str) -> str:
        from dourmouse.chat import ChatSession
        from dourmouse.dispatch import DispatchRegistry

        result = ChatSession(DispatchRegistry(), session_file=None).ask(prompt, force_plain_dispatch=True)
        return str(result.get("final_text") or "")
    return complete


def _json(text: str) -> dict[str, Any] | None:
    """The first complete JSON object in a reply. Models wrap JSON in prose
    and code fences, and prose AFTER the object can contain braces too
    (found live), so each candidate start is decoded to exactly one object
    rather than sliced to the last brace; fenced blocks are tried first."""
    decoder = json.JSONDecoder()
    fenced = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    for chunk in fenced + [text]:
        i = chunk.find("{")
        while i != -1:
            try:
                value, _ = decoder.raw_decode(chunk, i)
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
            i = chunk.find("{", i + 1)
    return None


def _hid(text: str) -> str:
    return "hyp-" + hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _question_claims(g: GraphStore, question_id: str) -> list[Any]:
    q = g.get("research_question", question_id)
    qs = [q] + g.related(q.ref, "decomposes_into")
    claims: list[Any] = []
    for qq in qs:
        for e in g.edges(dst=qq.ref, relation="answers"):
            if e.src_type == "claim":
                claims.append(g.get("claim", e.src_id))
    return claims


def generate_hypotheses(question_id: str, *, complete: Complete | None = None,
                        store: GraphStore | None = None) -> dict[str, Any]:
    """RES-13: hypotheses grounded in the question's own sourced claims."""
    g = store or GraphStore(_graph_db())
    q = g.get("research_question", question_id)
    claims = _question_claims(g, question_id)
    if not claims:
        return {"ok": False, "error": "this question has no claims yet: research it before hypothesising"}
    block = "\n".join(f"[{i}] {c.body['text']}" for i, c in enumerate(claims, 1))
    raw = (complete or default_complete())(_GENERATE.format(question=q.body["text"], claims=block))
    parsed = _json(raw) or {}
    made, dropped = [], 0
    for h in parsed.get("hypotheses") or []:
        if not isinstance(h, dict) or not isinstance(h.get("statement"), str) or not h["statement"].strip():
            dropped += 1
            continue
        refs = [r for r in (h.get("claims") or []) if isinstance(r, int) and not isinstance(r, bool)
                and 1 <= r <= len(claims)]
        if not refs:  # a hypothesis that rests on no real claim is not kept
            dropped += 1
            continue
        hid = _hid(h["statement"])
        g.put("hypothesis", hid, {"statement": h["statement"].strip(), "status": "proposed",
                                  "rationale": str(h.get("rationale") or "")[:2000]},
              created_by=BY, project_id=q.project_id)
        g.link(("hypothesis", hid), "about", ("research_question", question_id), created_by=BY)
        for r in refs:
            g.link(("hypothesis", hid), "derived_from", ("claim", claims[r - 1].id), created_by=BY)
        made.append({"id": hid, "statement": h["statement"].strip(), "claims": [claims[r - 1].id for r in refs]})
    if not made and not parsed:
        return {"ok": False, "error": "the model did not return hypotheses", "raw": raw[:1000]}
    return {"ok": True, "hypotheses": made, "dropped": dropped}


def criticize(hypothesis_id: str, *, complete: Complete | None = None,
              store: GraphStore | None = None) -> dict[str, Any]:
    """RES-14: a critic's review, recorded as a decision the hypothesis is
    revised by; its status becomes the critic's verdict."""
    g = store or GraphStore(_graph_db())
    h = g.get("hypothesis", hypothesis_id)
    raw = (complete or default_complete())(_CRITICIZE.format(statement=h.body["statement"],
                                                             rationale=h.body.get("rationale", "")))
    review = _json(raw)
    if not review or not all(isinstance(review.get(k), str) and review[k].strip() and not _PLACEHOLDER.match(review[k])
                             for k in ("weakest_assumption", "refuted_if")):
        # Found live: a model echoed the template ("...") back as its answer.
        return {"ok": False, "error": "the critic did not return a real review", "raw": raw[:1000]}
    verdict = review.get("verdict") if review.get("verdict") in ("plausible", "weak", "untestable") else "unclear"
    text = (f"Weakest assumption: {review['weakest_assumption']}\nAlternative: {review.get('alternative', '')}\n"
            f"Refuted if: {review['refuted_if']}\nVerdict: {verdict}")
    did = "dec-" + hashlib.sha256((hypothesis_id + text).encode("utf-8")).hexdigest()[:16]
    g.put("decision", did, {"summary": f"critic: {verdict}", "rationale": text}, created_by="critic",
          project_id=h.project_id)
    g.link(("hypothesis", hypothesis_id), "revised_by", ("decision", did), created_by="critic")
    if h.body.get("status") != f"criticized: {verdict}":
        g.revise("hypothesis", hypothesis_id, {"status": f"criticized: {verdict}"}, created_by="critic")
    return {"ok": True, "decision_id": did, "verdict": verdict, "review": review}


def design_and_run(hypothesis_id: str, *, complete: Complete | None = None, store: GraphStore | None = None,
                   wait_s: float = 120.0) -> dict[str, Any]:
    """RES-15 (+16): the model designs the experiment; it runs for real."""
    from . import experiments as ex

    g = store or GraphStore(_graph_db())
    h = g.get("hypothesis", hypothesis_id)
    raw = (complete or default_complete())(_DESIGN.format(statement=h.body["statement"]))
    design = _json(raw)
    if not design or not isinstance(design.get("protocol"), str) or not isinstance(design.get("code"), str):
        return {"ok": False, "error": "the model did not return a runnable design", "raw": raw[:1000]}
    code = re.sub(r"^```(?:python)?\s*|\s*```$", "", design["code"].strip())
    null = design.get("null_value")
    null_value = float(null) if isinstance(null, (int, float)) and not isinstance(null, bool) else None
    run = ex.run_experiment(design["protocol"], code, hypothesis_id=hypothesis_id, store=g, wait_s=wait_s,
                            project_id=h.project_id)
    out = {"ok": True, **{k: run[k] for k in ("experiment_id", "run_id", "state")}, "null_value": null_value}
    if run["state"] == "succeeded":
        # Without a stated null the test would be against 0, which is
        # meaningless for most hypotheses (found live: dice averages tested
        # against 0); then only the description is reported, no p-value.
        out["statistics"] = analyze_run(run["run_id"], store=g, job=run["job"],
                                        null_mean=null_value if null_value is not None else 0.0)
        if null_value is None and "error" not in out["statistics"]:
            for k in ("t", "p_two_sided", "null_mean"):
                out["statistics"].pop(k, None)
            out["statistics"]["method"] = "description only: the design stated no null value to test against"
    return out


# ---------------------------------------------------------------- RES-17
def describe_samples(samples: list[float], null_mean: float = 0.0) -> dict[str, Any]:
    """Deterministic statistics, no model: n, mean, standard deviation, a
    95% confidence interval for the mean, and a one-sample t statistic
    against ``null_mean`` with a two-sided p-value (normal approximation
    for large n, stated as such)."""
    xs = [float(x) for x in samples if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]
    n = len(xs)
    if n < 2:
        return {"n": n, "error": "at least two numeric samples are needed"}
    mean, sd = statistics.fmean(xs), statistics.stdev(xs)
    se = sd / math.sqrt(n)
    z = 1.959963984540054
    t = (mean - null_mean) / se if se > 0 else math.inf
    p = math.erfc(abs(t) / math.sqrt(2)) if math.isfinite(t) else 0.0
    return {"n": n, "mean": mean, "sd": sd, "ci95": [mean - z * se, mean + z * se], "null_mean": null_mean,
            "t": t, "p_two_sided": p, "method": "one-sample t with a normal approximation to its p-value"
            + (" (n < 30: treat p as rough)" if n < 30 else "")}


def analyze_run(run_id: str, *, store: GraphStore | None = None, job: dict[str, Any] | None = None,
                null_mean: float = 0.0) -> dict[str, Any]:
    """RES-17: statistics over a run's out/data.json samples, stored as a
    result about the run."""
    from dourmouse import compute_local

    g = store or GraphStore(_graph_db())
    g.get("experiment_run", run_id)
    if job is None:
        prefix = run_id[len("run-"):]
        job = next((j for j in compute_local.list_jobs(200) if j["id"].startswith(prefix)), None)
    if not job or "data.json" not in (job.get("artifacts") or {}):
        return {"error": "the run wrote no out/data.json with samples to analyse"}
    data = json.loads(compute_local.runner().artifact(job["id"], "data.json"))
    stats = describe_samples(data.get("samples") or [], null_mean)
    if "error" not in stats:
        rid = f"stats-{run_id}"
        summary = (f"n={stats['n']}, mean={stats['mean']:.6g}, sd={stats['sd']:.6g}, 95% CI "
                   f"[{stats['ci95'][0]:.6g}, {stats['ci95'][1]:.6g}], t={stats['t']:.4g}, p={stats['p_two_sided']:.4g}")
        g.put("result", rid, {"summary": summary, "data": stats}, created_by=BY)
        g.link(("result", rid), "about", ("experiment_run", run_id), created_by=BY)
    return stats
