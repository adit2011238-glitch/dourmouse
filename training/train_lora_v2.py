#!/usr/bin/env python3
"""train_lora_v2.py — SFT training entrypoint for the v2 dourmouse retrain.

Extended from the dourmouse-network scaffold's ``training/train_lora.py``
(same SFTTrainer + PEFT LoRA approach, same config shape) with one addition:
an optional validation set (``--valid-file`` / ``valid_file`` in the config)
passed to SFTTrainer as ``eval_dataset``, so the run reports eval loss on
the conversation-stratified held-out set (training_data/v2_valid.jsonl).

Run on the GPU machine (Linux + CUDA), not on the Mac:

  pip install -e ".[dev]"                      # dourmouse-network deps
  python training/train_lora_v2.py --config training_config/gpu_train_v2.yaml

Saves only the LoRA adapter (safetensors) + tokenizer to output_dir.
"""
import argparse
from pathlib import Path

import yaml
from datasets import load_dataset
from transformers import TrainingArguments
from trl import SFTTrainer

# Imported lazily inside main() so --help / config lint work without torch:
# from dourmouse_network import DourmouseModelConfig, load_model_for_training


SYSTEM_PROMPT = (
    "You are Dourmouse, a small-business workflow model. "
    "Return only valid JSON matching the Dourmouse output schema."
)


def format_example(example, tokenizer) -> str:
    messages = example["messages"]
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    return tokenizer.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=False)


def load_config(path: str) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the v2 Dourmouse LoRA adapter.")
    parser.add_argument("--config", default="training_config/gpu_train_v2.yaml")
    args = parser.parse_args()

    raw_config = load_config(args.config)

    from dourmouse_network import DourmouseModelConfig, load_model_for_training

    model_config = DourmouseModelConfig(
        base_model_name=raw_config["base_model_name"],
        use_4bit=raw_config["model"]["use_4bit"],
        torch_dtype=raw_config["model"]["torch_dtype"],
        device_map=raw_config["model"]["device_map"],
        lora_rank=raw_config["lora"]["rank"],
        lora_alpha=raw_config["lora"]["alpha"],
        lora_dropout=raw_config["lora"]["dropout"],
    )

    model, tokenizer = load_model_for_training(model_config)
    dataset = load_dataset("json", data_files=raw_config["train_file"], split="train")
    dataset = dataset.map(lambda example: {"text": format_example(example, tokenizer)})

    valid_file = raw_config.get("valid_file")
    eval_dataset = None
    if valid_file:
        eval_dataset = load_dataset("json", data_files=valid_file, split="train")
        eval_dataset = eval_dataset.map(
            lambda example: {"text": format_example(example, tokenizer)})

    train_config = raw_config["training"]
    training_args = TrainingArguments(
        output_dir=raw_config["output_dir"],
        num_train_epochs=train_config["num_train_epochs"],
        per_device_train_batch_size=train_config["per_device_train_batch_size"],
        gradient_accumulation_steps=train_config["gradient_accumulation_steps"],
        learning_rate=train_config["learning_rate"],
        warmup_ratio=train_config["warmup_ratio"],
        logging_steps=train_config["logging_steps"],
        save_steps=train_config["save_steps"],
        save_total_limit=train_config["save_total_limit"],
        eval_strategy=train_config.get("eval_strategy", "no"),
        eval_steps=train_config.get("eval_steps"),
        bf16=model_config.torch_dtype == "bfloat16",
        fp16=model_config.torch_dtype == "float16",
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        eval_dataset=eval_dataset,
        dataset_text_field="text",
        max_seq_length=train_config["max_seq_length"],
        args=training_args,
    )
    trainer.train()
    trainer.model.save_pretrained(raw_config["output_dir"])
    tokenizer.save_pretrained(raw_config["output_dir"])


if __name__ == "__main__":
    main()
