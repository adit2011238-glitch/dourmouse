import json
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, ".")

ROOT = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434"
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 8


def chat(payload):
    req = urllib.request.Request(
        ROOT + "/api/chat",
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
    task = ("List the current memory facts about the ATLAS pivot and the Phase 1 "
            "laptop deliverables with their status.")

    speeds = []
    walls = []
    for i in range(REPS):
        payload = {
            "model": "dourmouse-finetuned",
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
        tps = ev / evd * 1e9 if evd else 0
        speeds.append(tps)
        walls.append(wall)
        print(f"rep {i}: {ev:4d} tok in {wall:5.1f}s wall -> {tps:5.1f} t/s")
        time.sleep(0.5)

    print(f"\n{ROOT}  gen t/s: median {statistics.median(speeds):.1f} "
          f"mean {statistics.mean(speeds):.1f} min {min(speeds):.1f} max {max(speeds):.1f} "
          f"| wall: median {statistics.median(walls):.1f}s")


if __name__ == "__main__":
    main()
