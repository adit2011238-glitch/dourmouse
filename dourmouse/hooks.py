"""Deterministic hooks (Domain H, piece 4): pre-tool / post-tool / stop /
session -- the founding spec's own explicit hook-type breakdown, matching
Claude Code's PreToolUse/PostToolUse/Stop/SessionStart shape.

Real, minimal-risk wiring, not a new dispatch layer: pre-tool and post-tool
hooks register against dispatch.py's ONE existing call site for every real
tool invocation (`_execute_tool`, confirmed the sole caller of
`spec.handler(...)`); stop and session hooks register against chat.py's
`ChatSession` lifecycle boundaries (a turn finishing, a session starting/
ending). No caller anywhere needed to change to get hook support -- adding
a hook is calling `register_*_hook`, nothing else.

Two different real contracts, both deliberate:

- **pre-tool hooks CAN genuinely block** a tool call: returning a non-empty
  string is treated as a real denial reason, and the tool never runs (same
  shape as Claude Code's own PreToolUse "deny" outcome). This is the one
  hook type in this codebase that is allowed to affect execution.
- **every other hook type is a pure observer**, matching this codebase's
  established discipline for message_bus.on_post/ActivityTracker.on_event/
  office_logger.on_event: it can watch, it can never alter or abort.

In both cases, a RAISING hook is swallowed and treated as "no opinion" --
never a block, never a crash. A broken hook must never take down real
dispatch or a real chat session (Rule: same fail-open discipline this
codebase's other best-effort call sites already use).
"""

from __future__ import annotations

from typing import Any, Callable

PreToolHook = Callable[[str, dict[str, Any]], str | None]
PostToolHook = Callable[[str, dict[str, Any], str], None]
StopHook = Callable[[dict[str, Any]], None]
SessionHook = Callable[[str], None]

_pre_tool_hooks: list[PreToolHook] = []
_post_tool_hooks: list[PostToolHook] = []
_stop_hooks: list[StopHook] = []
_session_start_hooks: list[SessionHook] = []
_session_stop_hooks: list[SessionHook] = []


def register_pre_tool_hook(fn: PreToolHook) -> None:
    """``fn(tool_name, arguments) -> str | None``. A non-empty return value
    blocks the call; the string becomes the denial reason shown to the
    model. Return ``None`` (or raise) to allow."""
    _pre_tool_hooks.append(fn)


def register_post_tool_hook(fn: PostToolHook) -> None:
    """``fn(tool_name, arguments, result) -> None``. Fires after every real
    tool call (success or the handler's own error text), win or lose --
    a pure observer, cannot change ``result``."""
    _post_tool_hooks.append(fn)


def register_stop_hook(fn: StopHook) -> None:
    """``fn(report) -> None``, fired once a dispatch turn produces its
    final text -- ``report`` is the same dict ``run_dispatch_messages``/
    ``ChatSession.ask`` return (``final_text``, ``transcript``, ...)."""
    _stop_hooks.append(fn)


def register_session_start_hook(fn: SessionHook) -> None:
    """``fn(session_id) -> None``, fired once when a ``ChatSession`` is
    constructed -- ``session_id`` is its session file's stem."""
    _session_start_hooks.append(fn)


def register_session_stop_hook(fn: SessionHook) -> None:
    """``fn(session_id) -> None``, fired from ``ChatSession.close()``.

    Honest limitation, named not hidden: the webui.py server path keeps
    ONE ``ChatSession`` alive for the whole process lifetime (many HTTP
    turns, not one-session-per-request), so ``close()`` is never called on
    that path today -- only the REPL (``python -m dourmouse.chat``) calls
    it, in its own shutdown path. A future per-tab or per-connection
    session teardown could wire this in the webui too; not done here.
    """
    _session_stop_hooks.append(fn)


def clear_hooks() -> None:
    """Test isolation -- same convention as ``message_bus.set_message_bus
    (None)``: wipe every registered hook so one test's hooks can never leak
    into another's."""
    _pre_tool_hooks.clear()
    _post_tool_hooks.clear()
    _stop_hooks.clear()
    _session_start_hooks.clear()
    _session_stop_hooks.clear()


def run_pre_tool_hooks(tool_name: str, arguments: dict[str, Any]) -> str | None:
    for fn in list(_pre_tool_hooks):
        try:
            reason = fn(tool_name, arguments)
        except Exception:
            continue  # a broken hook has no opinion -- never a block
        if reason:
            return str(reason)
    return None


def run_post_tool_hooks(tool_name: str, arguments: dict[str, Any], result: str) -> None:
    for fn in list(_post_tool_hooks):
        try:
            fn(tool_name, arguments, result)
        except Exception:
            pass  # an observer must never break dispatch


def run_stop_hooks(report: dict[str, Any]) -> None:
    for fn in list(_stop_hooks):
        try:
            fn(report)
        except Exception:
            pass


def run_session_start_hooks(session_id: str) -> None:
    for fn in list(_session_start_hooks):
        try:
            fn(session_id)
        except Exception:
            pass


def run_session_stop_hooks(session_id: str) -> None:
    for fn in list(_session_stop_hooks):
        try:
            fn(session_id)
        except Exception:
            pass
