# Integration — forex-data research pipeline (v6.0)

This wires Dourmouse to the **real** forex research pipeline (`FOREX_DATA_PATH`,
typically `E:\forex-data`): the data inventory, the validated commodity-
seasonal strategy, the economic-calendar archive, the paper-trading log, and
the IBKR paper gateway. Everything is deterministic and honest — no LLM in
the path, no fabricated numbers (Rules 2.1 / 2.2 / 2.8).

## What was added

| Piece | File | What it does |
|---|---|---|
| `forex` subagent | `dourmouse/forex_ops.py` | Six tools, registered in `general_roster.py` |
| Morning report sections | `dourmouse/report.py` | Strategy + upcoming events + paper log in the daily briefing |
| Env template | `.env.example` | `FOREX_DATA_PATH`, `IBKR_HOST`, `IBKR_PORT` |

## The six tools (ask the `forex` agent)

- **`forex_inventory`** — normalized FX manifest (pairs × timeframes, D1
  coverage, bar counts, quality), commodity daily series count + date range,
  events-archive size, fundamentals files, newest reports under `reports/`.
- **`forex_strategy`** — the validated seasonal strategy: the verdict section
  of `reports/VALIDATION_REPORT.md`, plus the **live** paper calendar from
  `scripts/seasonal_calendar.py` (real subprocess output — shows NOW OPEN /
  UPCOMING windows).
- **`forex_events`** — upcoming high/medium-impact entries from
  `market-data/events/events.parquet` (default next 48h; skips already-
  released actuals). Reads via pandas → pyarrow → honest "not installed".
- **`forex_paper`** — `reports/paper_log.csv`: open positions, closed
  trades, realised P&L. Honest "no log yet" when absent.
- **`forex_ibkr`** — real 2-second TCP probe of the IBKR paper gateway
  (`IBKR_HOST:IBKR_PORT`, default `192.168.1.95:7497`). REACHABLE /
  UNREACHABLE with the socket error.
- **`forex_report`** — one consolidated block of all five.

## Setup

1. Copy `.env.example` → `.env` (already happens on first run).
2. Set:
   ```env
   FOREX_DATA_PATH=E:\forex-data
   IBKR_HOST=192.168.1.95
   IBKR_PORT=7497
   ```
   (Adjust the path if the pipeline lives elsewhere; leave `IBKR_*` as-is
   for the paper gateway on your other device.)
3. Nothing else. Until `FOREX_DATA_PATH` is set, every tool reports
   `NOT CONFIGURED` — by design, never a stub.

## Verify

```bash
python - <<'EOF'
import os
os.environ["FOREX_DATA_PATH"] = r"E:\forex-data"
from dourmouse.forex_ops import forex_inventory, forex_strategy, forex_events, forex_paper, forex_ibkr
print(forex_inventory())
print()
print(forex_strategy()["calendar"][:800])
print()
print(forex_events())
EOF
```

Or ask the roster: `RUN:GENERAL  show forex pipeline status` — the
`forex` agent resolves the tools itself.

## Tests

`dourmouse/tests/test_forex_ops.py` — hermetic (fake pipeline dir, no
network): NOT CONFIGURED degradation, inventory from a fake manifest,
paper-log parsing, calendar subprocess, IBKR probe via a monkeypatched
socket. Run: `python -m pytest dourmouse/tests/test_forex_ops.py -q`.

## ATLAS Terminal (v8.0) — the streamlit UI, now on real data

The terminal (`atlas_terminal/`, launched with `./start_atlas_ui.sh` or
`PORT=8510 ./start_atlas_ui.sh`) is the ATLAS Terminal UI upgraded from
mock data to the real pipeline:

- **`atlas_terminal/data.py`** — real-data layer over `forex_ops` + a parser
  for `reports/VALIDATION_REPORT.md` (leg stats, portfolio, core, bootstrap)
  + the live calendar + events + paper log + IBKR probe.
- **10 modules, zero mock numbers**: Command Center, Opportunity Radar
  (real trade windows + upcoming events), Research (real inventory +
  validation pipeline), Strategy Lab (the validated core legs), Portfolio
  (the $100 paper account), Risk (real drawdown/Sharpe/bootstrap), Alpha
  (leg attribution), News (real calendar + MarketAux headlines), AI Analyst
  (deterministic read of the validation record — no LLM yet), Execution
  (real IBKR probe + paper order book).
- **Honest by design**: with `FOREX_DATA_PATH` unset every module shows a
  NOT CONFIGURED state — the old mock NVDA/BTC numbers are gone.
- **`atlas_ui` roster agent**: `atlas_terminal_status` tool returns what the
  terminal shows right now, so dourmouse agents can answer "what's the
  terminal saying?" with the same data the UI renders.

Run it: `./start_atlas_ui.sh` → http://127.0.0.1:8501 (needs
`requirements-atlas-ui.txt` installed, done by the launcher on first run).

## Design rules honoured

- **No fabrication (2.2):** every tool reads real files or reports an
  honest failure / NOT CONFIGURED. The IBKR probe is a real TCP connect.
- **Deterministic (2.8):** filesystem + CSV reads, one subprocess
  (`seasonal_calendar.py`, stdout captured), a 2s socket timeout.
- **Hermetic tests (2.1):** no network, no real data, monkeypatched env.
- **Config (7):** paths and IBKR host/port come from env only.
