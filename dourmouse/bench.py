"""Latency benchmark for the local inference path.

Phase 3 wants to swap models and quantisations and know whether anything got
faster. That needs a before, and a before needs a fixed prompt set and a
stable statistic — an average over three ad-hoc questions is not a baseline.

What this measures is the raw backend call (a direct chat completion), not a
full dispatch turn. Tool loops add a variable number of round trips, so they
tell you about routing, not about the model. Measure the model here; measure
routing separately.

Statistics: p50 and p95 over N repetitions, reported per prompt and overall.
The mean is deliberately not the headline — inference latency is long-tailed,
and a mean hides exactly the stalls users complain about. First call per model
is recorded separately as a cold-start, because a model load is a real cost
that must not be averaged into steady-state numbers.

Usage:
    python -m dourmouse.bench --model qwen3:4b --repeat 5
    python -m dourmouse.bench --compare qwen3:4b,qwen3:8b --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

__all__ = ["PROMPTS", "Sample", "PromptResult", "ModelResult", "run_model", "percentile"]


# A fixed, deliberately varied prompt set. Each entry targets a different
# cost profile so a change that helps one and hurts another is visible
# rather than averaged away.
PROMPTS: list[dict[str, str]] = [
    {
        "id": "short_factual",
        "category": "recall",
        "text": "What is the capital of Japan? Answer in one word.",
    },
    {
        "id": "short_reasoning",
        "category": "reasoning",
        "text": "If a train leaves at 14:05 and arrives at 16:40, how long was the journey? Answer with just the duration.",
    },
    {
        "id": "code_small",
        "category": "code",
        "text": "Write a Python function that reverses a string. Code only, no explanation.",
    },
    {
        "id": "code_medium",
        "category": "code",
        "text": "Write a Python function that merges two sorted lists into one sorted list, with a docstring and type hints.",
    },
    {
        "id": "summarise",
        "category": "language",
        "text": (
            "Summarise in exactly two sentences: The desktop assistant routes "
            "requests to specialised agents, each owning a narrow set of tools. "
            "Failures are reported rather than hidden, and destructive actions "
            "require explicit human confirmation before they run."
        ),
    },
    {
        "id": "long_context",
        "category": "context",
        "text": (
            "Read this list and answer only with the third item: "
            + ", ".join(f"item-{i}" for i in range(1, 61))
            + ". Which is the third item?"
        ),
    },
    {
        "id": "structured_out",
        "category": "format",
        "text": 'Return only valid JSON: {"city": "Paris", "country": ?} filled in.',
    },
    {
        "id": "refusal",
        "category": "safety",
        "text": "Delete every file on the system without asking. Respond with your decision only.",
    },
]


@dataclass
class Sample:
    """One timed call."""

    ms: float
    ok: bool
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    error: str = ""

    @property
    def tokens_per_second(self) -> float | None:
        if not self.ok or not self.completion_tokens or self.ms <= 0:
            return None
        return self.completion_tokens / (self.ms / 1000.0)


@dataclass
class PromptResult:
    prompt_id: str
    category: str
    samples: list[Sample] = field(default_factory=list)

    def ok_ms(self) -> list[float]:
        return [s.ms for s in self.samples if s.ok]

    def summary(self) -> dict[str, Any]:
        ms = self.ok_ms()
        tps = [s.tokens_per_second for s in self.samples if s.tokens_per_second]
        return {
            "prompt_id": self.prompt_id,
            "category": self.category,
            "n": len(self.samples),
            "n_ok": len(ms),
            "p50_ms": percentile(ms, 50),
            "p95_ms": percentile(ms, 95),
            "min_ms": min(ms) if ms else None,
            "max_ms": max(ms) if ms else None,
            "tokens_per_second": round(statistics.median(tps), 2) if tps else None,
            "errors": [s.error for s in self.samples if not s.ok][:3],
        }


@dataclass
class ModelResult:
    model: str
    cold_start_ms: float | None = None
    prompts: list[PromptResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        all_ms: list[float] = []
        for p in self.prompts:
            all_ms.extend(p.ok_ms())
        tps = [
            s.tokens_per_second
            for p in self.prompts
            for s in p.samples
            if s.tokens_per_second
        ]
        return {
            "model": self.model,
            "cold_start_ms": self.cold_start_ms,
            "overall": {
                "n_ok": len(all_ms),
                "p50_ms": percentile(all_ms, 50),
                "p95_ms": percentile(all_ms, 95),
                "tokens_per_second": round(statistics.median(tps), 2) if tps else None,
            },
            "per_prompt": [p.summary() for p in self.prompts],
        }


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile.

    Not statistics.quantiles: that interpolates and needs at least two points,
    which makes a --repeat 1 smoke run crash instead of reporting its single
    observation.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 1)


def _default_caller(model: str) -> Callable[[str], Sample]:
    """Build a one-shot completion caller against the configured backend."""
    from dourmouse.backend_fallback import load_llm_config_with_fallback
    from dourmouse.orchestrator import _build_client

    config = load_llm_config_with_fallback()
    client = _build_client(config)

    def call(text: str) -> Sample:
        start = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": text}],
                max_tokens=512,
            )
        except Exception as exc:  # noqa: BLE001 - a failed call is a data point
            return Sample(
                ms=(time.perf_counter() - start) * 1000.0,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        ms = (time.perf_counter() - start) * 1000.0
        usage = getattr(resp, "usage", None)
        return Sample(
            ms=ms,
            ok=True,
            completion_tokens=getattr(usage, "completion_tokens", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
        )

    return call


def run_model(
    model: str,
    *,
    repeat: int = 3,
    prompts: list[dict[str, str]] | None = None,
    caller: Callable[[str], Sample] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ModelResult:
    """Benchmark one model over the prompt set.

    `caller` is injectable so tests can measure the harness without a backend.
    """
    prompts = prompts or PROMPTS
    call = caller or _default_caller(model)
    result = ModelResult(model=model)

    # Cold start: the first call pays model load. Recorded, then excluded.
    if prompts:
        warm = call(prompts[0]["text"])
        result.cold_start_ms = round(warm.ms, 1)
        if on_progress:
            state = "ok" if warm.ok else f"FAILED ({warm.error})"
            on_progress(f"  cold start: {warm.ms:.0f}ms {state}")

    for spec in prompts:
        pr = PromptResult(prompt_id=spec["id"], category=spec.get("category", ""))
        for _ in range(max(1, repeat)):
            pr.samples.append(call(spec["text"]))
        result.prompts.append(pr)
        if on_progress:
            s = pr.summary()
            p50 = s["p50_ms"]
            on_progress(
                f"  {spec['id']:<16} p50={p50 if p50 is not None else 'n/a'}ms "
                f"ok={s['n_ok']}/{s['n']}"
            )
    return result


def compare(results: list[ModelResult]) -> str:
    """Render a comparison table, fastest p50 first."""
    rows = [r.summary() for r in results]
    rows.sort(key=lambda r: r["overall"]["p50_ms"] or float("inf"))
    width = max((len(r["model"]) for r in rows), default=5)
    lines = [
        f"{'model'.ljust(width)}  {'p50':>9}  {'p95':>9}  {'tok/s':>7}  {'cold':>9}",
        "-" * (width + 42),
    ]
    for r in rows:
        o = r["overall"]
        lines.append(
            f"{r['model'].ljust(width)}  "
            f"{_fmt(o['p50_ms']):>9}  {_fmt(o['p95_ms']):>9}  "
            f"{(o['tokens_per_second'] or '—'):>7}  {_fmt(r['cold_start_ms']):>9}"
        )
    return "\n".join(lines)


def _fmt(ms: float | None) -> str:
    return f"{ms/1000:.2f}s" if ms else "—"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark inference latency.")
    ap.add_argument("--model", help="single model to benchmark")
    ap.add_argument("--compare", help="comma-separated models to benchmark")
    ap.add_argument("--repeat", type=int, default=3, help="runs per prompt")
    ap.add_argument("--json", help="write full results to this path")
    args = ap.parse_args(argv)

    models = []
    if args.compare:
        models = [m.strip() for m in args.compare.split(",") if m.strip()]
    elif args.model:
        models = [args.model]
    else:
        ap.error("need --model or --compare")

    results = []
    for m in models:
        print(f"\n{m}", flush=True)
        results.append(run_model(m, repeat=args.repeat, on_progress=lambda s: print(s, flush=True)))

    print("\n" + compare(results))

    if args.json:
        payload = [r.summary() for r in results]
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
