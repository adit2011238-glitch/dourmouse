"""Conversational front end (Claude-Cowork-style) for the General Dispatch Agent.

Turn a single-shot dispatch into a persistent, multi-turn conversation:
- ChatSession keeps the full OpenAI-format message list (system + history) so
  the NVIDIA model has context across turns — the same tool loop from
  dispatch.py, but the caller owns the message list.
- Every turn is persisted as a JSONL audit record under
  <workspace>/sessions/<timestamp>.jsonl, and the full message state is
  snapshotted to <same>.messages.json so a session can be RESUMED later
  (reference-project style state persistence, adapted).
- The REPL (`python -m dourmouse.chat`) is the Cowork-like terminal:
  type prompts, get answers, approve/decline confirmation-gated tools with
  y/N. One-shot mode: `python -m dourmouse.chat "prompt"`.

All anti-fabrication rules apply unchanged (Rules 2.1/2.2/2.9): gated tools
need the human gate, unconfigured backends report NOT CONFIGURED, and the
assistant never claims to have sent/executed anything the transcript didn't
show.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dourmouse.config import NvidiaConfig
from dourmouse.dispatch import (
    DispatchRegistry,
    JobTracker,
    run_dispatch_messages,
    system_message,
)
from dourmouse.governance import BudgetTracker, DlpFilter, RbacPolicy
from dourmouse.learn import learn_enabled, recall_block
from dourmouse.memory_store import MemoryStore

# Delay general_roster import so chat.py stays importable for engine tests
# without pulling in every tool backend.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_sessions_dir() -> Path:
    # Reuse the workspace root convention from general_roster without
    # importing it (avoids a cycle): env var wins, else <project>/workspace.
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


class ChatSession:
    """A persistent multi-turn conversation against a DispatchRegistry.

    ``messages`` is the authoritative OpenAI-format history (system first).
    ``ask()`` appends the user turn, runs the tool loop (which appends the
    assistant's reply and any tool exchanges in place), persists an audit
    record + state snapshot, and returns the dispatch report.
    """

    def __init__(
        self,
        registry: DispatchRegistry,
        session_file: Path | None = None,
        client: Any | None = None,
        config: NvidiaConfig | None = None,
        confirmation_gate: Callable[[str], bool] | None = None,
        job_tracker: JobTracker | None = None,
        cost_budget: BudgetTracker | None = None,
        dlp: DlpFilter | None = None,
        rbac: RbacPolicy | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.registry = registry
        # v2.9 Store & Learn: ``memory`` is the long-term store the learning
        # loop reads/writes. None (default) disables learning entirely so
        # engine tests and non-learning callers behave exactly as before.
        self.memory = memory
        self._base_system = system_message(registry)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._base_system}
        ]
        self.client = client
        self.config = config
        self.confirmation_gate = confirmation_gate
        self.job_tracker = job_tracker or JobTracker()
        # Institutional governance, owned at SESSION level so a conversation's
        # budget/dlp/role persist across every turn (spec: cost-capping and
        # role-based access control are session-scoped, not per-request).
        self.cost_budget = cost_budget or BudgetTracker()
        self.dlp = dlp if dlp is not None else DlpFilter()
        self.rbac = rbac if rbac is not None else RbacPolicy()
        # Per-conversation role switches (Phase A3), each audited as a
        # role_changes event in the ledger.
        self.role_changes: list[dict[str, Any]] = []
        self._prev_hash: str | None = None
        if session_file is None:
            session_file = (
                _default_sessions_dir()
                / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
        self.session_file = Path(session_file)
        self._state_file = self.session_file.with_suffix(".messages.json")
        self._turn_count = 0
        self._load_state()
        self._prev_hash = _last_record_hash(self.session_file)

    # ------------------------------------------------------------------ #
    # Conversation
    # ------------------------------------------------------------------ #

    def ask(
        self,
        prompt: str,
        max_turns: int = 8,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send one user turn; returns the dispatch report.

        The report's ``messages`` key is THIS session's updated history.
        ``event_sink`` streams each transcript event as it is produced so a
        UI can show tool activity live. ``model`` (v3.1) overrides the model
        for THIS turn only — e.g. a focus_agent route runs that agent's own
        NVIDIA model. None uses the session default.
        """
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("ask() requires a non-empty prompt")
        # Wall-time budget is per request-tree, not per session: restart the
        # clock at the start of every turn. Without this, a session that has
        # been alive longer than max_wall_seconds (600s default) would reject
        # EVERY new directive instantly with BUDGET EXHAUSTED — the web UI's
        # single long-lived ChatSession hits this after ten minutes of uptime.
        # Calls and cost remain session-scoped and cumulative.
        self.cost_budget.reset_wall_clock()
        self.messages.append({"role": "user", "content": prompt})
        # v2.9 Store & Learn: before each turn, deterministically recall the
        # stored knowledge most relevant to THIS prompt and inject it into the
        # system message, so the model actually uses what it learned from past
        # sessions. No matches -> the base system message is left exactly as
        # is (never a stale recall block from a previous turn).
        if self.memory is not None and learn_enabled():
            block = recall_block(self.memory, prompt)
            if block:
                # KV-cache stability (v4.2 speed): messages[0] stays the
                # immutable base prompt, so the stable prefix (system +
                # history) is reused between turns instead of re-prefilling
                # from scratch whenever recall fires. The recalled context is
                # injected as its OWN trailing system message, just before
                # the new directive — the model reads it as context, and the
                # bounded window in dispatch.py always keeps it.
                self.messages.insert(-1, {"role": "system", "content": block})
        started = time.monotonic()
        report: dict[str, Any] = {"final_text": "", "transcript": []}
        try:
            report = run_dispatch_messages(
                self.messages,
                self.registry,
                max_turns=max_turns,
                client=self.client,
                config=self.config,
                confirmation_gate=self.confirmation_gate,
                event_sink=event_sink,
                job_tracker=self.job_tracker,
                cost_budget=self.cost_budget,
                dlp=self.dlp,
                rbac=self.rbac,
                model=model,
            )
        except Exception:
            # Keep the history well-formed even on API failure (mirrors the
            # max_turns fix in dispatch.py): a bare "user" tail would make the
            # next turn send consecutive user messages, which some
            # OpenAI-compatible backends reject.
            self.messages.append({"role": "assistant", "content": ""})
            raise
        finally:
            # Persist even when the API call raises, so a resumed session
            # never silently loses the turn that was already in memory.
            self._turn_count += 1
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            self._persist(prompt, report, elapsed_ms)
            # v2.9 Store & Learn: auto-ingest the completed turn into the
            # long-term store (idempotent upsert). Only when the turn actually
            # completed with an answer — we never learn from a failed turn
            # with an empty final_text. A store that breaks mid-run must not
            # take down the conversation.
            if (
                self.memory is not None
                and learn_enabled()
                and report.get("final_text")
            ):
                try:
                    self.memory.ingest_session_file(self.session_file)
                except Exception:
                    pass
        return report

    def history(self) -> list[dict[str, Any]]:
        """Read-only view of the OpenAI-format conversation history."""
        return list(self.messages)

    # ------------------------------------------------------------------ #
    # Per-conversation RBAC (Phase A3)
    # ------------------------------------------------------------------ #

    def set_role(self, role: str) -> dict[str, Any]:
        """Switch this conversation's RBAC role, audited as an event.

        Applies from the NEXT turn onward (the in-flight turn keeps its
        original role — atomicity, no mid-turn privilege change). Invalid
        roles raise loudly; the attempted switch is NOT recorded.
        """
        policy = RbacPolicy(role)  # raises ValueError on unknown role
        previous = self.rbac.role
        self.rbac = policy
        self.role_changes.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "from": previous,
                "role": role,
            }
        )
        return policy.snapshot()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load_state(self) -> None:
        """Resume prior message state if this session file was used before.

        The registry may have grown new tools since the snapshot was taken, so
        the CURRENT system message (persona + roster) is re-injected while the
        conversational history is kept.
        """
        if not self._state_file.exists():
            return
        try:
            loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt state must never silently break a session.
            raise RuntimeError(
                f"cannot resume session state from {self._state_file}: {exc}"
            )
        if loaded and loaded[0].get("role") == "system":
            loaded[0] = {"role": "system", "content": system_message(self.registry)}
            # Keep _base_system in sync so recall rebuilds from the CURRENT
            # roster, never from a recalled snapshot.
            self._base_system = loaded[0]["content"]
        # v4.2: drop stale recall blocks persisted by OLDER turns — recall is
        # re-injected fresh (as its own trailing system message) on the next
        # ask(), so a resumed session must not carry recalled facts from
        # turns ago. messages[0] is the freshly re-injected base above and is
        # never a recall block, so this only removes the trailing ones.
        loaded = [
            m
            for m in loaded
            if not (
                m.get("role") == "system"
                and "REMEMBERED CONTEXT" in (m.get("content") or "")
            )
        ]
        self.messages = loaded
        self._turn_count = sum(1 for m in self.messages if m["role"] == "user")

    def _persist(self, prompt: str, report: dict[str, Any], elapsed_ms: float) -> None:
        """Append one hash-chained, tamper-evident audit record (spec:
        immutable audit trail & logging).

        Each record carries the SHA-256 of the previous record (``prev_hash``)
        plus its own ``hash`` over all fields except ``hash`` itself, so any
        edit to any past record breaks every subsequent link — detectable with
        ``verify_session_audit()``. Also records wall-clock latency and every
        human intervention (confirmation requested/resolved) from the turn.
        """
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        interventions = [
            e
            for e in report.get("transcript", [])
            if e.get("type") in ("confirmation_requested", "confirmation_resolved")
        ]
        record: dict[str, Any] = {
            "turn": self._turn_count,
            "timestamp": datetime.now().isoformat(),
            "elapsed_ms": elapsed_ms,
            "user": prompt,
            "final_text": report.get("final_text", ""),
            "interventions": interventions,
            "role_changes": list(self.role_changes),
            "transcript": report.get("transcript", []),
            "prev_hash": self._prev_hash,
        }
        record["hash"] = _record_hash(record)
        with self.session_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self._prev_hash = record["hash"]
        # Snapshot the full message state for resumability.
        self._state_file.write_text(json.dumps(self.messages))


# --------------------------------------------------------------------------- #
# Tamper-evident audit ledger (spec: immutable audit trail & logging)
# --------------------------------------------------------------------------- #

def _record_hash(record: dict[str, Any]) -> str:
    """SHA-256 of a record's canonical JSON, excluding its own ``hash`` field."""
    payload = json.dumps(
        {k: v for k, v in record.items() if k != "hash"},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_record_hash(session_file: Path) -> str | None:
    """The ``hash`` of the last record in an existing session file, or None."""
    if not session_file.is_file():
        return None
    last_hash: str | None = None
    try:
        for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("hash"):
                last_hash = rec["hash"]
    except (json.JSONDecodeError, OSError):
        return None
    return last_hash


def export_audit(session_file: Path | str, out_path: Path | str) -> tuple[bool, list[str]]:
    """Export a session ledger to a validated, ordered copy (Phase A2).

    Verifies the hash chain first; if the ledger is intact, writes every
    record to ``out_path`` in original order. Returns ``(ok, errors)`` — a
    tampered ledger is NOT exported (never propagate possibly-altered data
    to a compliance store).
    """
    ok, errors = verify_session_audit(session_file)
    if not ok:
        return False, errors
    src = Path(session_file)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    records = [ln for ln in src.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    with out.open("w", encoding="utf-8") as fh:
        for line in records:
            fh.write(line + "\n")
    return True, []


def verify_session_audit(session_file: Path | str) -> tuple[bool, list[str]]:
    """Verify a session JSONL is unbroken (hash chain intact).

    Returns ``(ok, errors)`` where ``errors`` lists every tampered or
    out-of-order record. This is the compliance check: any edit to any past
    record, or a reordered/duplicated/removed record, is detected.
    """
    path = Path(session_file)
    if not path.is_file():
        return False, [f"no session file: {path}"]
    errors: list[str] = []
    prev_hash: str | None = None
    records = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        records += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: not valid JSON: {exc}")
            continue
        actual = _record_hash(rec)
        if rec.get("hash") != actual:
            errors.append(f"line {lineno}: hash mismatch (record was edited)")
        if rec.get("prev_hash") != prev_hash:
            errors.append(
                f"line {lineno}: prev_hash broken (record missing/reordered/edited)"
            )
        prev_hash = rec.get("hash")
    if records == 0:
        return False, ["empty session file"]
    return not errors, errors


# --------------------------------------------------------------------------- #
# REPL
# --------------------------------------------------------------------------- #

def _print_turn(report: dict[str, Any]) -> None:
    for entry in report.get("transcript", []):
        if entry["type"] == "tool_use":
            print(f"  🛠  tool: {entry['name']}({entry['raw_arguments']})")
        elif entry["type"] == "tool_result":
            preview = entry["text"][:200].replace("\n", " ")
            print(f"  ↳ result: {preview}")
    print(f"  🤖 {report.get('final_text', '')}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    import argparse

    from dourmouse.dispatch import _cli_confirmation_gate
    from dourmouse.general_roster import build_general_registry

    parser = argparse.ArgumentParser(
        prog="atlas-dourmouse chat",
        description="Cowork-style conversational front end for the General Dispatch Agent.",
    )
    parser.add_argument(
        "prompt", nargs="*", help="one-shot prompt; omit to enter the REPL"
    )
    parser.add_argument(
        "--session",
        default=None,
        help="session file path (default: <workspace>/sessions/session_<ts>.jsonl)",
    )
    parser.add_argument(
        "--verify",
        metavar="SESSION",
        default=None,
        help="verify a session ledger's hash chain (Phase A2); exit 0 ok / 1 broken",
    )
    parser.add_argument(
        "--export",
        nargs=2,
        metavar=("SESSION", "OUT"),
        default=None,
        help="export a VERIFIED session ledger to a file (Phase A2)",
    )
    args = parser.parse_args(argv)

    from dourmouse.chat import export_audit, verify_session_audit

    if args.verify is not None:
        ok, errors = verify_session_audit(args.verify)
        print(f"LEDGER: {args.verify}")
        if ok:
            print("STATUS: VERIFIED — hash chain intact (tamper-evident).")
            return 0
        print("STATUS: TAMPERED — chain broken:")
        for e in errors:
            print(f"  ! {e}")
        return 1

    if args.export is not None:
        ok, errors = export_audit(args.export[0], args.export[1])
        if ok:
            print(f"EXPORTED verified ledger to: {args.export[1]}")
            return 0
        print("EXPORT REFUSED — ledger did not verify:")
        for e in errors:
            print(f"  ! {e}")
        return 1

    registry = build_general_registry()
    session = ChatSession(
        registry,
        session_file=Path(args.session) if args.session else None,
        confirmation_gate=_cli_confirmation_gate,
    )
    print(f"Session file: {session.session_file}")
    print("Available subagents:")
    print(registry.describe_roster())

    if args.prompt:
        report = session.ask(" ".join(args.prompt))
        _print_turn(report)
        return 0

    print("\nType a request, or: exit/quit to leave.")
    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        if user_input.lower() in {"exit", "quit"}:
            print("bye")
            return 0
        if not user_input:
            continue
        report = session.ask(user_input)
        _print_turn(report)


if __name__ == "__main__":
    sys.exit(main())
