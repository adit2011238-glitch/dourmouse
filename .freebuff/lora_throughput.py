import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from dourmouse.dispatch import system_message
from dourmouse.general_roster import build_general_registry


def chat(payload):
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = resp.read().decode()
    wall = time.time() - t0
    return json.loads(data), wall


def main():
    reg = build_general_registry()
    sys_prompt = system_message(reg)
    task = "List the current memory facts about the ATLAS pivot and the Phase 1 laptop deliverables with their status."
    for model in ["dourmouse-finetuned", "qwen2.5:7b"]:
        for rep in range(2):
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": task},
                ],
                "stream": False,
                "think": False,
                "enable_thinking": False,
                "keep_alive": "30m",
                "options": {"num_predict": 500, "num_ctx": 8192},
            }
            d, wall = chat(payload)
            ev = d.get("eval_count", 0)
            evd = d.get("eval_duration", 0)
            gen_tps = ev / evd * 1e9 if evd else 0
            print(f"{model:22s} rep{rep}: wall {wall:5.1f}s | gen {ev:4d} tok @ {gen_tps:5.0f} t/s")
            time.sleep(1)


if __name__ == "__main__":
    main()
