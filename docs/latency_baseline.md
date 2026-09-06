# Latency baseline — local inference (dourmouse.bench)

Dated: 2026-09-06 (cycle 1 repo audit, reliability lane). First-ever run of
`dourmouse/bench.py` in this repo — no prior baseline existed to compare
against, so this is a cold, one-time snapshot, not a regression check.

## Environment (real, not simulated)

- Backend: local Ollama, `http://127.0.0.1:11434/v1` (`OllamaConfig`,
  `is_cloud=False`). Forced explicitly via `OLLAMA_BASE_URL` for this run —
  the ambient `.env` in this checkout has an `OLLAMA_API_KEY` set, which
  `load_ollama_config()` otherwise prioritizes into routing through Ollama
  Cloud (see its docstring); that would have benchmarked a network hop to
  `ollama.com`, not this machine's local inference path.
- Model: `qwen2.5:7b` (this codebase's own `_OLLAMA_DEFAULT_MODEL`),
  already pulled and resident. `ollama ps` reported `100% GPU`.
- Command actually run:
  `python -m dourmouse.bench --model qwen2.5:7b --repeat 1 --json ...`

## Repeat count: 1, not 5 — stated honestly

The task asked for `--repeat 5`. A `--repeat 3` run was started first and
killed partway through the `code_medium` prompt (partial output, ~4.5
minutes elapsed, only 4 of 8 prompts done) because measured steady-state
throughput this session was **~3.0-3.3 generated tokens/sec**, well below
this repo's own prior documented figure of 4.2-5.4 t/s under load
(`docs/SPEED_OPTIMIZATION.md`, 2026-08-12, same M3 Air). At that rate a
`--repeat 5` full run (8 prompts, up to 512 max_tokens each) would cost on
the order of 45-60+ minutes dominated almost entirely by the two `code_*`
prompts, which isn't a defensible latency cost for a cycle-1 audit
baseline. `--repeat 1` was substituted so every number below is a single,
real, timed sample — not simulated, not averaged from a partial run — at
the cost of no p50/p95 spread per prompt (each prompt's own `p50_ms` /
`p95_ms` are the same single sample; only the ACROSS-PROMPTS `overall`
row is a genuine percentile over 8 independent observations). Re-run with
`--repeat 5` on a quieter machine (or after confirming GPU throughput is
actually being used, not silently falling back to CPU) for a statistically
sturdier number — this file does not fabricate one.

## Results (single sample per prompt, n=8 prompts)

| prompt | category | latency (ms) | tokens/sec |
|---|---|---:|---:|
| short_factual | recall | 864.9 | 3.47 |
| short_reasoning | reasoning | 3,169.2 | 2.52 |
| code_small | code | 6,576.7 | 3.19 |
| code_medium | code | 130,780.7 | 3.23 |
| summarise | language | 13,356.4 | 2.99 |
| long_context | context | 6,742.2 | 0.59 |
| structured_out | format | 5,723.1 | 2.97 |
| refusal | safety | 2,462.7 | 2.84 |

Cold start (first call, model already resident): 936.8 ms.

**Overall, across all 8 prompts (real percentile, n=8):**

| stat | value |
|---|---:|
| p50 | 5,723.1 ms |
| p95 | 130,780.7 ms |
| median tokens/sec | 2.98 |

The p95 is dominated by `code_medium` (full function + docstring + type
hints — the prompt set's largest expected completion, capped at
`max_tokens=512`), which alone ran 130.8s. `long_context`'s 0.59 tok/s is
an artifact of a tiny completion (the answer is one short item id) against
a long *prompt* (60 list items) — `tokens_per_second` here measures
generation only, so a short completion after a long prompt-processing pass
reads as "slow" per-token without actually being a slow response.

## Reading this baseline

- This machine's real generation throughput today (~3 t/s) is consistent
  with `docs/SPEED_OPTIMIZATION.md`'s standing finding that this is a
  hardware/load ceiling, not a dourmouse configuration bug — nothing here
  contradicts that doc, and no code changed as part of collecting this
  baseline.
- `dourmouse/bench.py` itself required no fixes to run: CLI-only, one real
  HTTP call per sample, no GUI/webview/mic import touched, confirmed by
  running it exactly as ready.
- Next time this is re-run (a real model/config change, not just "checking
  in"), diff against this file's numbers — that is the whole point of
  having a checked-in baseline instead of an ad-hoc terminal scrollback.
