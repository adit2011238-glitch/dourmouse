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
- **In-sample bias — addressed 2026-08-12**: a committed, conversation-
  stratified held-out split now exists: 9 conversations / 94 pairs (10%) →
  `training_data/held_out.jsonl` (seed 42), 849 pairs remain for training.
  Held-out benchmark (`training_data/held_out_benchmark.md`): strict-JSON
  **5/5 (100%)** on never-seen conversations — format obedience was NOT
  memorization; refusal 1/8 (same as in-sample); avg latency 14.7 s. B-
  coverage stayed weak (0.03) on both runs — a methodology artifact: Part B
  sends bare user fragments without the conversation context the reference
  answers were written in, so the model (correctly) asks clarifying
  questions instead of matching them. See also
  `docs/NETWORK_SCAFFOLD_VERDICT.md` for the training-system decision.
- **Advanced Path (orch_net.py)**: not yet seeded with the labeled data —
  status unverified.

## 6. v2 retraining run — dataset + GPU command (ready to go)

A real retraining run is prepared, gated only on a GPU machine (the Mac has
no HF stack; bitsandbytes is excluded on Darwin, so 4-bit training cannot
run here). Build everything locally, run on the GPU:

```bash
# 1) build the v2 dataset (local, reproducible):
.venv/bin/python scripts/build_v2_dataset.py
#    -> training_data/v2_train.jsonl  (877 rows: 849 real dispatcher pairs +
#       28 pivot action-label rows covering all 13 contract action types)
#    -> training_data/v2_valid.jsonl (94 held-out rows, same chat format)

# 2) on the GPU machine (Linux + CUDA, 24GB+ VRAM):
cd /path/to/dourmouse-network          # scaffold package: pip install -e ".[dev]"
cp /Volumes/ATLAS\ /Atlas/dourmouse-4.0.0/training_config/gpu_train_v2.yaml .
cp /Volumes/ATLAS\ /Atlas/dourmouse-4.0.0/training/train_lora_v2.py .
cp /Volumes/ATLAS\ /Atlas/dourmouse-4.0.0/training_data/v2_train.jsonl .
cp /Volumes/ATLAS\ /Atlas/dourmouse-4.0.0/training_data/v2_valid.jsonl .
python training/train_lora_v2.py --config gpu_train_v2.yaml
#    LoRA adapter (safetensors) + tokenizer -> artifacts/dourmouse-lora-v2
```

`train_lora_v2.py` is the dourmouse-network scaffold's SFTTrainer entrypoint
(``training/train_lora.py``) plus an ``eval_dataset`` on the held-out set.
Config highlights: base **Qwen/Qwen2.5-7B-Instruct** (the verified
production base — NOT the scaffold's 3B default), 4-bit + bf16, rank 16 /
alpha 32 (TRAINING_PLAN spec), 3 epochs, batch 4, lr 2e-4, max_seq_length
8192 (longest row ≈ 3.8k tokens with the roster prompt).

Post-train on the GPU machine:

```bash
# convert the adapter to GGUF for Ollama (llama.cpp convert_lora_to_gguf),
then on the Mac:
ollama create dourmouse-finetuned-v2 -f Modelfile  # Modelfile: FROM qwen2.5:7b + ADAPTER
# validate on the held-out set with the Phase 1.2 harness:
cd atlas-strategy-lab && python3 scripts/model_benchmark.py \
    --pairs ../training_data/held_out.jsonl --system-prompt /tmp/dourmouse_system_prompt.txt \
    --models dourmouse-finetuned-v2 --n 8 --out reports/MODEL_BENCHMARK_V2.md
```

Expected best case: held-out strict-JSON ≥ the current 5/5 and a model that
also answers pivot action-contract prompts — that is the retrain's new
capability. Honest risk: mixing dispatcher + pivot formats in one dataset
can degrade the dispatcher's format discipline; validate before promoting.

## 7. Files

```
Modelfile                          portable model definition
lora-dourmouse-claude.gguf         23.1 MB LoRA adapter (exported from Ollama)
TRAINING_RECIPE.md                 this doc
training_data/                     943 pairs / 82 labeled conversations / domains
  held_out.jsonl                   94-pair conversation-stratified split (seed 42)
  v2_train.jsonl                   877 rows (build via scripts/build_v2_dataset.py)
  v2_valid.jsonl                   94 held-out rows in chat format
scripts/build_v2_dataset.py        v2 dataset builder (reproducible)
scripts/ingest_claude_training.py  RAG ingest (Immediate Path)
training/train_lora_v2.py          eval-enabled SFTTrainer entrypoint (GPU)
training_config/gpu_train_v2.yaml  GPU training config for the v2 run
```
