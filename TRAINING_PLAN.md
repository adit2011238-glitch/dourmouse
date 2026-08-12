# TRAINING PLAN — dourmouse from Claude Export

Generated from **82 Claude conversations** (8.9 MB export) spanning **2026-04-26 → 2026-08-09**.
Labeled into **943 instruction pairs** across 3 domains:

| Domain    | Conversations | Turns | % of data |
|-----------|:------------:|:-----:|:---------:|
| Finance    | 77            | 2007  | ~94%      |
| General    | 4             | 86    | ~5%       |
| Coding     | 1             | 2     | ~1%       |

## Immediate Path (RAG injection — today, no training needed)

**dourmouse's `MemoryStore`** (SQLite FTS5 full-text retrieval) already has an
`ingest_session_file` that indexes conversation turns for recall. The labeled
data fits this same shape.

1. **Convert instruction pairs → session format** — the `instruction_pairs.jsonl`
   pairs are already close to dourmouse's own session JSONL format. A small
   adapter (`scripts/ingest_claude_training.py`) reads each pair, wraps it as
   a session turn, and calls `store.ingest_session_file()` on a temp file.
2. **Inject into system prompt** — the store's `search(query)` feeds top
   FTS5 matches into the system message as `REMEMBERED CONTEXT`, so dourmouse
   automatically recalls relevant Claude conversations when you ask something
   similar (e.g., "how did I configure that backtest in March?").
3. **Result with zero training:** dourmouse can draw on ALL 943 instruction
   pairs as searchable knowledge — no model weights changed, no GPU needed.

**Why this first:** it works immediately, requires no GPU, adds zero latency
(FTS5 recall takes <5ms), and improves deterministically with more data.

## Medium Path (LoRA fine-tune — improves behavior, not just recall)

The 943 instruction pairs are **question → answer** pairs — ideal for
supervised fine-tuning (SFT). The local stack is:

| Component | Location | Purpose |
|-----------|----------|---------|
| Ollama    | `localhost:11434` | Hosts the inference model (qwen3:8b) |
| `modelfile` | `dourmouse/Modelfile` (to create) | Quantized LoRA adapter definition |
| `unsloth` / `llama.cpp` | installable | LoRA training on the export pairs |

**Step-by-step fine-tune:**

1. Convert `instruction_pairs.jsonl` into a JSONL with the model's chat template:
   ```json
   {"messages": [{"role": "system", "content": "..."},
                 {"role": "user", "content": "..."},
                 {"role": "assistant", "content": "..."}]}
   ```
2. Fine-tune with LoRA (unsloth or `llama.cpp` `llama-fine-tune`):
   - Target: `qwen3:8b` (or a smaller test with `qwen3:4b`)
   - Rank 16, alpha 32, target modules: q_proj, v_proj
   - 3 epochs, batch size 4, lr 2e-4
   - Output: a Safetensors LoRA adapter (~20 MB)
3. Export to GGUF and create an Ollama Modelfile:
   ```
   FROM qwen3:8b
   ADAPTER ./lora-qwen3-claude.gguf
   TEMPLATE {{ .System }}
   ```
4. `ollama create dourmouse-claude -f Modelfile`
5. Point `DOURMOUSE_MODEL_AGENT` or `DOURMOUSE_MODEL_ORCHESTRATOR` to the new
   model: `dourmouse-claude`.

**Why LoRA over full fine-tune:** 943 pairs (~6 MB text) is small data. A
full fine-tune would overfit. LoRA captures the domain-specific behavior
(quant/atlas/backtest planning) without catastrophic forgetting.

## Advanced Path (neural orchestrator pattern learning)

`dourmouse/orch_net.py` (v5.6) builds a graph network over the session history
to learn patterns. The Claude export can seed this:

1. Convert each labeled conversation into a graph episode (user_intent →
   tools_used → assistant_response → user_feedback).
2. Feed into `orch_net.train(episodes)` — the network learns which tool
   sequences succeed for which domain (e.g., finance → _gmail_search + 
   stock_quote + build_morning_report; coding → _run_python + git_dispatch).
3. The orchestrator then selects tool chains proactively rather than relying
   entirely on LLM prompt variance.

**Requires:** `orch_net.py`'s `train()` API (may need a thin adapter to accept
the labeled format). The Claude export's 943 pairs provide ~2000 episodes
(each turn is one episode) — a strong seed.

## Recommended Order

1. **Today: RAG ingest** → dourmouse recalls Claude knowledge immediately
2. **This week: LoRA fine-tune** → specializes the local model for your domain
3. **Next: orchestator seed** → dourmouse learns tool-chain patterns

## Files

```
training_data/
  labeled_conversations.jsonl   — 82 labeled entries (domain, tags, metadata)
  instruction_pairs.jsonl       — 943 (user → assistant) pairs for SFT
  summary.json                  — domain breakdown + stats
  domains/
    finance.txt                 — 77 finance conversations (concatenated)
    general.txt                 — 4 general conversations
    coding.txt                  — 1 coding conversation
```

## Appendix: Domain Classification

Domains are assigned by keyword heuristics (not LLM — deterministic, per
Rule 2.8). The `scripts/label_claude_export.py` script runs the classifer.
To re-classify with a different scheme, modify `_DOMAIN_HEURISTICS` and
re-run. To add more data, drop additional Claude exports into:
```
/claude_export/conversations.json + re-run label_claude_export.py
```