#!/usr/bin/env python3
"""Run the PL-10 T1 research benchmark against the real paper services and the
configured cloud model, in an isolated workspace.

    .venv/bin/python scripts/research_benchmark.py --items spacing,testing --out /tmp/bench

Writes results.json (every measurement, every answer) and summary.md (the
table) to --out. Nothing is approved on the owner's behalf: the benchmark's
questions are fixed by benchmark_questions.json. Keyless OpenAlex allows about
a hundred searches a day per network address and keyless Semantic Scholar is
often rate limited; a run says which services failed instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--items", default="", help="comma-separated item ids (default: all ten)")
    ap.add_argument("--out", required=True, help="directory for results.json and summary.md")
    ap.add_argument("--max-papers", type=int, default=8, help="papers read per item before follow-ups")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--workspace", default="", help="isolated workspace (default: a new temporary folder)")
    args = ap.parse_args()

    os.environ["DOURMOUSE_WORKSPACE"] = args.workspace or tempfile.mkdtemp(prefix="dm-bench-ws-")
    from dourmouse.research_pipeline import benchmark as BM
    from dourmouse.research_pipeline.cycle import Budget
    from dourmouse.research_pipeline.hypotheses import default_complete
    from dourmouse.research_pipeline.library_port import InterimLibrary
    from dourmouse.research_pipeline.papers import PaperClient

    wanted = [x.strip() for x in args.items.split(",") if x.strip()]
    items = [i for i in BM.load_items() if not wanted or i["id"] in wanted]
    if not items:
        print("no matching items", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    budget = Budget(initial_papers=args.max_papers, max_papers=args.max_papers + 6, max_rounds=args.max_rounds)
    library = InterimLibrary()
    results = []
    for item in items:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {item['id']}: {item['question']}", flush=True)
        client = PaperClient()
        try:
            res = BM.run_item(item, client=client, complete=default_complete(), library=library,
                              now_year=datetime.now(timezone.utc).year, budget=budget)
        except Exception as exc:  # noqa: BLE001 -- one failed item is reported, the rest still run
            res = {"id": item["id"], "question": item["question"], "error": f"{type(exc).__name__}: {exc}"}
            print(f"  FAILED: {res['error']}", flush=True)
            results.append(res)
            continue
        p, r = res["precision"], res["recall"]
        print(f"  precision {p['verified']}/{p['claims']}, found {r['found']}/{r['references']}, read "
              f"{r['read']}/{r['references']}, {res['model_calls']} model calls, {res['seconds']} s, "
              f"stop: {res['stop_reason']}", flush=True)
        results.append(res)
    ok = [r for r in results if "error" not in r]
    (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = BM.summary_markdown(ok) if ok else "No item finished.\n"
    failed = [r for r in results if "error" in r]
    if failed:
        summary += "\nFailed items:\n" + "".join(f"- {r['id']}: {r['error']}\n" for r in failed)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
