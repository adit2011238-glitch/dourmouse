# dourmouse-network scaffold — verdict (2026-08-12)

**Decision: USE the structured-output contract and the training-entrypoint pattern;
DISCARD the scaffold as the live training system for dourmouse.**

The package at `/Users/aditagrawal/neural network for dourmouse/dourmouse-network`
(v0.1.0, built 2026-08-12) is a **scaffold, by its own admission**: the plan doc
says "No local model training is run by this task", the README says the training
command is "for the future training machine, not required for this scaffold",
and all five tasks are checked off as package/skeleton work. Nothing was trained
and no model was downloaded.

## Why it is not adopted as the live training system

1. **Wrong base model for the dispatcher role.** It defaults to
   `Qwen/Qwen2.5-3B-Instruct`. The Phase 1.2 benchmark (2026-08-12) mapped the
   reliability cliff on this hardware: qwen3:8b 60% strict-JSON, gemma4:12b 20%,
   versus dourmouse-finetuned 100%. A 3B dispatcher carrying the 9,190-char
   roster prompt would land below both — faster, but below the product's core
   promise ("the system that refuses to fool you").
2. **Wrong runtime.** Live dourmouse runs on Ollama (GGUF base + GGUF LoRA,
   `dourmouse-finetuned` = qwen2.5:7b + 23.1 MB LoRA). The scaffold trains
   HF/transformers PEFT adapters (safetensors), which Ollama cannot load without
   a conversion step. As shipped it **cannot run on this Mac at all**:
   `use_4bit: true` requires bitsandbytes, which the scaffold's own pyproject
   excludes on Darwin, and `device_map: auto` is CUDA-oriented (macOS needs
   `{"": "mps"}`).
3. **Wrong data.** Six hand-written example rows vs the 943 real instruction
   pairs (from 82 labeled Claude conversations) that produced the current model.
4. **Wrong contract for the live brain.** Its output contract
   (`assistant_message` / `action` / `needs_confirmation` / `confidence`, with
   Gmail/Calendar/Docs/Sheets/Drive/Spotify actions) is the **pivot product's**
   action model — small-business workflow automation — not the current
   dispatcher's tool-call contract.

## What is kept from it

- **The fail-closed structured-output contract + parser + JSON Schema.** A model
  output that does not validate must never trigger an action — ask the user
  instead. This is correct, and it aligns with the commercial pivot (and with
  desktop's decision-card machine-readable schema direction). Adopt the *shape*
  of this contract as the spec for the Phase 0/1 commercial action model.
- **The SFTTrainer entrypoint + YAML config pattern** as the template for the
  future GPU training machine — exactly what the scaffold's own docs intend.

## Speed optimization — measured results (this session)

Probes against the live `dourmouse-finetuned` with the production roster prompt
(9,190 chars / ~2,117 tokens):

- **Prompt caching works.** First call: 11.9 s prompt eval (178 tok/s cold).
  Subsequent turns: 0.1 s (25k+ tok/s cache hit). The roster prompt is **not**
  the per-turn bottleneck.
- **Generation is the bottleneck:** ~10–17 tok/s (thermal-bound on the fanless
  M3 Air), 8–15 s per typical turn (200–300 tokens).
- **The LoRA is a speed win, not a cost:** finetuned 15 tok/s with 118–165-token
  outputs vs plain qwen2.5:7b at 6–9 tok/s with 196–222 tokens.
- **Applied change:** `_DEFAULT_MAX_TOKENS` 1400 → 800 in `dourmouse/dispatch.py`
  (measured outputs run 120–300 tokens; 800 keeps 2.7× headroom and halves
  worst-case generation latency; code completions keep their own 4,000 cap).
  80/80 dispatch + local-llm tests pass.
- Existing architecture already good: fast lane routes pure-chat turns to
  qwen3:4b + compact prompt; keep_alive 30m; num_ctx 8192; thinking disabled.

## Training continuation

- **Held-out split created** (seeded, conversation-stratified — no leakage):
  9 conversations / 94 pairs (10%) → `training_data/held_out.jsonl`; 849 pairs
  remain for training. Fixes the documented in-sample-bias caveat of the Phase
  1.2 benchmark.
- **Held-out benchmark (done):** strict-JSON **5/5 (100%)** on tasks from
  conversations the model never saw — the format obedience was **not
  memorization**. Refusal 1/8 (same as in-sample); avg latency 14.7 s (warm
  cache). B-coverage stayed weak (0.03) on both runs — a benchmark
  methodology artifact: Part B sends bare user fragments without the
  conversation context the reference answers were written in, so the model
  (correctly) asks clarifying questions instead of matching them. Full
  detail: `training_data/held_out_benchmark.md`.
- Rebuild recipe already committed (`Modelfile`, `TRAINING_RECIPE.md`,
  `lora-dourmouse-claude.gguf`).
- When a GPU machine exists, train the pivot action model with the scaffold's
  entrypoint **on the real data pipeline** (943 pairs extended with pivot
  action labels + the fail-closed contract), never on the 6 example rows.
