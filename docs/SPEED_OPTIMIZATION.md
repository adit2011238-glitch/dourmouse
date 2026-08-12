# SPEED OPTIMIZATION — dourmouse on Ollama (M3 Air, 2026-08-12)

**Bottom line: the Ollama stack is already at its configurable ceiling.**
Generation throughput is hardware/system-load bound, not configuration bound.
The one applied change this session (`_DEFAULT_MAX_TOKENS` 1400 → 800,
`dourmouse/dispatch.py`) bounds worst-case latency; everything else measured
as already-optimal or not-helpful.

## Measured generation throughput (dourmouse-finetuned, production roster prompt)

| condition | gen t/s |
|---|---|
| quiet machine, earlier today | ~15 t/s (dips to 10) |
| under current concurrent load (load avg ~31) | **4.2–5.4 t/s (median 4.5)** |

The same binary, same config, same prompt: 15 vs 4.5 t/s. The delta is
concurrent system load (browser/WebKit, Grammarly, Freebuff, heavy exFAT disk
I/O) competing for the M3 Air's unified-memory bandwidth and throttling a
fanless chassis. A typical dispatcher turn: ~15 s quiet, ~50 s loaded.

## Already optimal (verified in the running server)

- **KV cache quantization**: `OLLAMA_KV_CACHE_TYPE=q8_0` + llama-server
  `--cache-type-k q8_0 --cache-type-v q8_0` — already on.
- **Flash attention**: `--flash-attn auto` — already on.
- **Prompt caching**: the 9,190-char roster prompt is 12 s cold, **0.1 s
  cached** (25k+ tok/s cache hit) — measured, works.
- **keep_alive 30m** (server env + dispatch pins it), `OLLAMA_MAX_LOADED_MODELS=3`.
- **num_ctx 8192** — required: the dispatch bounded context is 4,600 tokens.
- **num_predict 800** — applied this session (measured outputs 120–300 tokens).

## Tested this session — no improvement

- **`OLLAMA_SCHED_SPREAD=1`** (second isolated server, port 11435, same model
  store): median 4.2 t/s vs 4.5 t/s baseline — within noise. Not worth applying.
- **Adapter quantization**: the LoRA is a 23 MB GGUF; quantizing it would cut
  <1% of per-token memory bandwidth vs the 4.7 GB base. Non-lever.

## Not available in Ollama

- **Speculative decoding / draft model**: not exposed by the Ollama server
  (no `--model-draft`, no env var). Would require a custom llama.cpp server —
  out of scope for the live stack.

## What actually moves the number

1. **Reduce concurrent load during heavy turns** (close heavy browser tabs /
   Grammarly-class apps) — the same model does 15 t/s quiet vs 4.5 t/s loaded.
2. **Cooldown between sustained runs**: the fanless Air thermally throttles;
   sustained benchmarks show 5.4 → 4.2 t/s drift within minutes.
3. **The fast lane already routes pure-chat to qwen3:4b** (~2.5 GB, several ×
   faster than the 7B) with a compact prompt — the biggest real speed win in
   the stack and it predates this session.
4. **A smaller dispatcher model would be the only config-free speedup left** —
   rejected on benchmark evidence (qwen3:8b 60% / gemma4:12b 20% strict-JSON
   vs the finetuned 7B at 100%).
