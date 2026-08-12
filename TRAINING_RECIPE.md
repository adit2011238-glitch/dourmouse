# TRAINING RECIPE — dourmouse-finetuned (rebuildable from this repo)

**Purpose:** make the finetuned production model reproducible. The model itself
was validated in `atlas-strategy-lab/reports/MODEL_BENCHMARK.md` (Phase 1.2):
100% strict-JSON obedience vs qwen3:8b 60% / gemma4:12b 20%; 33 s avg latency.
This doc records exactly what it is and how to rebuild it.

---

## 1. What the model is (verified 2026-08-12)

| Property | Value | How verified |
|---|---|---|
| Model name | `dourmouse-finetuned:latest` | `ollama list` |
| **Base** | **`qwen2.5:7b`** | manifest base blob `sha256:2bada8a745067` matches qwen2.5:7b's manifest exactly (NOT qwen3:8b as TRAINING_PLAN originally suggested) |
| **Adapter** | **LoRA, 23,084,160 bytes (23.1 MB)** | exported to `./lora-dourmouse-claude.gguf` from blob `sha256:adfc9535e803f…` |
| Architecture | qwen2, 7.6 B params, Q4_K_M, ctx 32768 | `ollama show` |
| Template | Qwen chat template with `<tool_call>` function calling (from base) | `ollama show --modelfile` |
| SYSTEM | Base default ("You are Qwen…") — no custom system | `ollama show --modelfile` |
| PARAMETERs | none custom | `ollama show --modelfile` |
| Created | ~2026-08-11 (18 h before first check) | `ollama list` modified time |

## 2. Training data (in this repo)

- 82 Claude conversations, 2026-04-26 → 2026-08-09 (export)
- 2,095 turns → **943 instruction pairs** (`training_data/instruction_pairs.jsonl`)
- Domain split: finance 94% (77 convos), general 5% (4), coding 1% (1) — `training_data/summary.json`
- RAG half: 807 `claude_export` facts ingested into the live MemoryStore (FTS5 recall — the "Immediate Path")

## 3. How it was built (Medium Path of TRAINING_PLAN.md)

1. Convert `instruction_pairs.jsonl` to chat-template JSONL
   (`{"messages":[{"role":"system",…},{"role":"user",…},{"role":"assistant",…}]}`).
2. LoRA fine-tune on the **qwen2.5:7b** base (TRAINING_PLAN spec: rank 16,
   alpha 32, target `q_proj`+`v_proj`, 3 epochs, batch 4, lr 2e-4 — exact run
   params not preserved in-repo; the plan values are the documented spec).
3. Export the LoRA adapter to GGUF (the 23.1 MB file at
   `./lora-dourmouse-claude.gguf`).
4. `ollama create dourmouse-finetuned -f Modelfile` with
   `FROM qwen2.5:7b` + `ADAPTER ./lora-dourmouse-claude.gguf`.

## 4. Rebuild from this repo (fully reproducible)

```bash
# base present?
ollama list | grep qwen2.5:7b            # else: ollama pull qwen2.5:7b
# rebuild the model from the committed adapter + Modelfile
ollama create dourmouse-finetuned -f Modelfile
# verify
ollama show dourmouse-finetuned          # base qwen2.5:7b, adapter present
```

**That's it.** The adapter and Modelfile are committed, so the model is
rebuildable on a fresh machine with Ollama + qwen2.5:7b pulled — no training
run, GPU, or original export needed.

## 5. Honest limits & follow-ups

- **Training run itself is not fully reproducible**: the LoRA *training* step
  (hyperparameters, tool) was ad hoc; the committed adapter preserves the
  *result*, but retraining from data requires the step-2 script, which is not
  committed. Next: commit a `scripts/train_lora.py` (unsloth/llama.cpp)
  wrapper around the spec in §3.2.
- **In-sample bias**: the benchmark sampled from the same 943 pairs the model
  was trained on → its 100% is an upper bound. A true held-out validation
  needs data collected *after* training (or a committed train/test split made
  before the run). The Phase 1.2 follow-up is to build that split + harness.
- **Advanced Path (orch_net.py)**: not yet seeded with the labeled data —
  status unverified.

## 6. Files

```
Modelfile                          portable model definition
lora-dourmouse-claude.gguf         23.1 MB LoRA adapter (exported from Ollama)
TRAINING_RECIPE.md                 this doc
training_data/                     943 pairs / 82 labeled conversations / domains
scripts/ingest_claude_training.py  RAG ingest (Immediate Path)
```
