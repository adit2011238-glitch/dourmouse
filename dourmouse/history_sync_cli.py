"""Scheduled-task entrypoint: `python -m dourmouse.history_sync_cli`.

Run periodically (a Windows scheduled task, same convention as the
DOURMOUSE-Desktop server task itself) to pull fresh Claude Code / Codex
history and import it into long-term memory without the user having to
click the SETTINGS panel's manual button every time.

Reads DOURMOUSE_HISTORY_SYNC_* from the user's persistent config
(``%LOCALAPPDATA%\\Dourmouse\\.env`` on Windows — see
``dourmouse.config.user_env_path``, loaded automatically the moment
``dourmouse.config`` is imported, which is why that import happens here
even though nothing in this file calls it directly). If unconfigured,
exits 0 — a machine that never had this set up must not fail a scheduled
task, it just has nothing to do (Rule 2.2: honest, not fabricated).
"""

from __future__ import annotations

import sys

import dourmouse.config  # noqa: F401 -- import triggers load_dotenv, see module docstring
from dourmouse.history_sync import sync_and_import
from dourmouse.learn import default_store_path
from dourmouse.memory_store import MemoryStore, MemoryStoreUnavailable


def main() -> int:
    try:
        store = MemoryStore(default_store_path())
    except MemoryStoreUnavailable as exc:
        print(f"history sync: memory store not configured: {exc}")
        return 0

    try:
        result = sync_and_import(store)
    finally:
        store.close()

    if result.get("reason") == "not_configured":
        print("history sync: DOURMOUSE_HISTORY_SYNC_* not set, nothing to do")
        return 0
    if not result.get("ok"):
        print(f"history sync: {result.get('reason', 'failed')} — will retry next run")
        return 0  # a scheduled task failing loudly on a transient network
        # miss would be noisier than useful; the marker didn't advance,
        # so the next scheduled run picks up from the same point.

    claude = result.get("claude", {})
    codex = result.get("codex", {})
    print(
        f"history sync: ok — claude {claude.get('imported', 0)} imported "
        f"({claude.get('scanned', 0)} scanned, {result.get('pulled', 0)} pulled) — "
        f"codex {codex.get('imported', 0)} imported ({codex.get('scanned', 0)} scanned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
