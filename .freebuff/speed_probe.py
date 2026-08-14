import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

MODEL = "dourmouse-finetuned"


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
    from dourmouse.dispatch import system_message
    from dourmouse.general_roster import build_general_registry

    reg = build_general_registry()
    sys_prompt = system_message(reg)
    print(f"system prompt: {len(sys_prompt)} chars")

    tasks = [
        "Summarize the current memory facts about the ATLAS pivot and list the Phase 1 laptop deliverables with their status.",
        "Draft a short daily reliability log entry for the 14-day gate.",
    ]

    for i, task in enumerate(tasks):
        for rep in range(3):  # same task 3x to observe caching
            payload = {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": task},
                ],
                "stream": False,
                "think": False,
                "enable_thinking": False,
                "keep_alive": "30m",
                "options": {"num_predict": 400, "num_ctx": 8192},
            }
            d, wall = chat(payload)
            ev = d.get("eval_count", 0)
            evd = d.get("eval_duration", 0)
            pe = d.get("prompt_eval_count", 0)
            ped = d.get("prompt_eval_duration", 0)
            gen_tps = ev / evd * 1e9 if evd else 0
            pre_tps = pe / ped * 1e9 if ped else 0
            print(
                f"task{i} rep{rep}: wall {wall:6.1f}s | prompt {pe:5d} tok "
                f"@ {pre_tps:5.0f} t/s ({ped/1e9:5.1f}s) | gen {ev:4d} tok "
                f"@ {gen_tps:5.0f} t/s ({evd/1e9:5.1f}s)"
            )
            time.sleep(1)


if __name__ == "__main__":
    main()
