#!/usr/bin/env python3
"""LoRA fine-tune on safety refusals dataset (68 examples).

Trains on 1.5B quantized model via Ollama, using adapter_config for LoRA.
Minimal deps: peft, transformers, torch. Runs on MX110 (2GB VRAM) at bs=1.

Output: adapter saved to training_data/lora_adapter/, ready to merge.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import torch
    import transformers
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install: pip install peft transformers torch")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_safety_data(path: str = "training_data/safety_refusals.jsonl") -> list[dict]:
    """Load training examples (question, refusal answer)."""
    examples = []
    with open(path) as f:
        for line in f:
            examples.append(json.loads(line))
    return examples


def finetune():
    """LoRA fine-tune on safety data."""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    # Use the largest Ollama model already present (qwen3:8b or qwen2.5:7b)
    model_name = "Qwen/Qwen2.5-7B"  # or find locally; this is for reference
    print(f"Loading model: {model_name}")

    # Simplified: quantized 7B won't fit in 2GB even at q4.
    # Fallback: use TinyLLaMA or a 1.5B model from HuggingFace.
    # For now, use a manageable model.
    model_name = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        load_in_8bit=True,
        device_map="auto",
        torch_dtype=torch.float16,
    )

    # LoRA config: tiny adapter for a 1.1B model
    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    # Load safety data
    examples = load_safety_data()
    print(f"Loaded {len(examples)} training examples")

    # Format for training: "Q: {prompt}\nA: {refusal}\n"
    texts = []
    for ex in examples:
        prompt = ex.get("prompt", "")
        refusal = ex.get("refusal", "")
        text = f"Q: {prompt}\nA: {refusal}\n"
        texts.append(text)

    # Tokenize
    encodings = tokenizer(
        texts,
        truncation=True,
        max_length=512,
        padding=True,
        return_tensors="pt",
    )

    # Simple training loop (no trainer framework, manual backward)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    model.train()

    epochs = 3
    bs = 1
    n_batches = (len(examples) + bs - 1) // bs

    print(f"Training: {epochs} epochs, batch_size={bs}, {n_batches} batches/epoch")

    for epoch in range(epochs):
        total_loss = 0
        for i in range(0, len(examples), bs):
            batch_end = min(i + bs, len(examples))
            batch_size = batch_end - i

            input_ids = encodings["input_ids"][i:batch_end].to(device)
            attention_mask = encodings["attention_mask"][i:batch_end].to(device)

            outputs = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                labels=input_ids,
            )
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            total_loss += loss.item()
            if (i // bs + 1) % max(1, n_batches // 5) == 0:
                print(f"  Epoch {epoch+1}/{epochs}, batch {i//bs+1}/{n_batches}, loss={loss.item():.4f}")

        avg_loss = total_loss / n_batches
        print(f"Epoch {epoch+1} done. Avg loss: {avg_loss:.4f}")

    # Save adapter
    out_dir = Path("training_data/lora_adapter")
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    print(f"LoRA adapter saved to {out_dir}")
    print(f"Merge: from peft import AutoPeftModelForCausalLM; model = AutoPeftModelForCausalLM.from_pretrained(...)")


if __name__ == "__main__":
    finetune()
