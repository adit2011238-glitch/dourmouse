# Auto-sync with upstream dourmouse

When new features are pushed to `github.com/adit2011238-glitch/dourmouse`,
this checkout updates itself — **without ever clobbering the local
integration work** (the `forex` agent, `atlas_ui` agent, `atlas_terminal/`
UI, morning-report sections, tests).

## How it runs

| Trigger | Frequency |
|---|---|
| Windows scheduled task `DourmouseAutoSync` | every 30 minutes |
| `./start.sh` (Linux/macOS launcher) | at startup |

Both just run `tools/sync_dourmouse.py`, so they behave identically.
Run it manually any time:

```bash
python tools/sync_dourmouse.py
```

## What it does (merge-safe by construction)

1. `git fetch origin` — never rewrites history, never force-pushes,
   never touches git config (identity is passed per-invocation).
2. If upstream has new commits → `git merge origin/main`. Our local
   commits (the v8.0 integration) are merged alongside upstream's
   features — both sides survive.
3. **On a conflict it ABORTS the merge** (exit 2), leaves the tree
   untouched, and logs `CONFLICT` to `sync_log.txt` for manual
   resolution. It never auto-resolves or discards either side.
4. After a successful merge it re-installs any requirement file that
   changed (`requirements.txt`, `requirements-dev.txt`,
   `requirements-atlas-ui.txt`) into the local `.venv`.
5. It runs the integration test set (forex_ops, atlas_terminal,
   dispatch, atlas_ops, report) and logs the result.
6. Every run is appended to `sync_log.txt` (gitignored).

## Verified paths (tested on a scratch clone, real checkout untouched)

- **Clean merge**: upstream change merged; both upstream's edit and our
  local `FOREX_DATA_PATH` block present afterwards. Exit 0.
- **Conflict**: overlapping edits in `dourmouse/report.py` → merge
  aborted, exit 2, working tree clean, HEAD unchanged, honest log line.

## Exit codes

- `0` — up to date, or synced + tests ok
- `2` — merge conflict (aborted, nothing clobbered — resolve manually)
- `3` — error (fetch failed, not a repo, …)

## After a sync

- The ATLAS Terminal (streamlit) hot-reloads changed files on the next
  browser refresh — no restart needed.
- If the dourmouse HUD/daemon is running, restart it to pick up new
  Python code (`./start.sh` re-runs the sync first).
- Review `sync_log.txt` after any merge for the pytest line.
