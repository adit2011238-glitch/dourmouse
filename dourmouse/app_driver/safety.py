"""Kill switch, the driving indicator, the typed-text secret check, and audit.

Kill switch: a module flag plus a file flag at ``<config>/app_driver/KILL``.
Either one stops driving. The file lets another process (the Electron shell,
or the owner with ``touch``) stop it without reaching this process. It is
checked before every action and between typed chunks. Engaging it is open to
everyone, the model included; releasing it is an owner-only HTTP route.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import AppDriverError

_killed = False
_kill_file_error: str | None = None
_kill_lock = threading.Lock()


def kill_file() -> Path:
    from dourmouse.config import user_config_dir

    return user_config_dir() / "app_driver" / "KILL"


def is_killed() -> bool:
    if _killed:
        return True
    try:
        return kill_file().exists()
    except OSError:
        return True  # cannot tell, so stopped is the safe answer


def kill_info() -> dict[str, Any]:
    info: dict[str, Any] = {"engaged": is_killed(), "memory_flag": _killed, "file": str(kill_file()), "file_error": _kill_file_error}
    with contextlib.suppress(OSError, ValueError):
        info.update({k: v for k, v in json.loads(kill_file().read_text(encoding="utf-8")).items() if k in ("at", "reason", "by")})
    return info


def engage_kill(reason: str = "", by: str = "") -> dict[str, Any]:
    global _killed, _kill_file_error
    with _kill_lock:
        _killed = True
        path = kill_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at": time.time(), "reason": reason[:200], "by": by[:60]}), encoding="utf-8")
            _kill_file_error = None
        except OSError as exc:
            # The memory flag already stops this process. The file is only the
            # cross-process copy, so its failure is reported, not fatal.
            _kill_file_error = f"{type(exc).__name__}: {exc}"
    end_action()
    audit("executed", "kill", {"reason": reason}, by, engaged=True)
    _notify()
    return kill_info()


def release_kill(by: str = "") -> dict[str, Any]:
    global _killed, _kill_file_error
    with _kill_lock:
        try:
            kill_file().unlink(missing_ok=True)
        except OSError as exc:
            raise AppDriverError("failed", f"could not remove the kill file {kill_file()}: {exc}") from exc
        _killed = False
        _kill_file_error = None
    audit("executed", "resume", {}, by, engaged=False)
    _notify()
    return kill_info()


def require_not_killed() -> None:
    if is_killed():
        raise AppDriverError("killed", "app driving is stopped by the kill switch. Only the owner can resume it.")


# Indicator.

LINGER_SECONDS = 10.0
_indicator_lock = threading.Lock()
_state: dict[str, Any] = {"active": False, "app": None, "action": None, "since": None, "last_action_at": None}
_listeners: list[Callable[[dict[str, Any]], Any]] = []
_recent: deque[dict[str, Any]] = deque(maxlen=50)


def indicator() -> dict[str, Any]:
    with _indicator_lock:
        state = dict(_state)
    last = state.get("last_action_at")
    lingering = bool(last) and (time.time() - float(last)) < LINGER_SECONDS
    state["driving"] = bool(state["active"]) or (lingering and not is_killed())
    state["label"] = f"Model is driving {state['app']}" if state["driving"] and state.get("app") else ""
    return state


def add_indicator_listener(fn: Callable[[dict[str, Any]], Any]) -> None:
    _listeners.append(fn)


def remove_indicator_listener(fn: Callable[[dict[str, Any]], Any]) -> None:
    with contextlib.suppress(ValueError):
        _listeners.remove(fn)


def _notify() -> None:
    snapshot = indicator()
    snapshot["killed"] = is_killed()
    for fn in list(_listeners):
        with contextlib.suppress(Exception):  # a UI listener must never break an action
            fn(snapshot)


def begin_action(app: str, action: str) -> None:
    now = time.time()
    with _indicator_lock:
        _state.update({"active": True, "app": app, "action": action, "since": now, "last_action_at": now})
    _notify()


def end_action() -> None:
    with _indicator_lock:
        if _state["active"]:
            _state["active"] = False
            _state["last_action_at"] = time.time()
    _notify()


def recent_actions() -> list[dict[str, Any]]:
    return list(_recent)


# Audit.

def audit(kind: str, action: str, arguments: dict[str, Any], actor: str = "", **extra: Any) -> None:
    """Record through the existing action ledger (execution_policy.record).
    Arguments are hashed there, never stored raw, so typed text never lands in
    the log. A bounded local copy (without arguments) feeds the status route."""
    from dourmouse import execution_policy

    execution_policy.record(kind, f"app_driver.{action}", arguments, actor or "", **extra)
    _recent.append({"at": time.time(), "kind": kind, "action": action, "actor": actor or "", **{k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool)) or v is None}})


# Secret check for typed text.

_LABEL_RE = re.compile(r"\bpass(word|code|phrase)?\b|passwd|\bpin\b|secret|api[ _-]?key|token|2fa|one[- ]time|otp|cvv|cvc|card number", re.I)


def looks_like_password(text: str) -> bool:
    """A single unbroken token that mixes character classes like a password,
    or a long letters-and-digits token like a key. Sentences pass."""
    token = text.strip()
    if not token or any(c.isspace() for c in token):
        return False
    classes = sum((any(c.islower() for c in token), any(c.isupper() for c in token),
                   any(c.isdigit() for c in token), any(not c.isalnum() for c in token)))
    if 8 <= len(token) <= 128 and classes >= 3:
        return True
    return len(token) >= 20 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def check_typed_text(text: str, element: dict[str, Any] | None) -> None:
    """Raise if this text must not be typed, or must not go into this field."""
    from dourmouse.governance import DlpFilter

    if element is not None:
        if element.get("secure"):
            raise AppDriverError("secret", "the target is a secure (password) field; the model never types into one.")
        label = " ".join(str(element.get(k) or "") for k in ("title", "description", "placeholder"))
        if _LABEL_RE.search(label):
            raise AppDriverError("secret", f"the target field is labelled {label.strip()[:60]!r}, which reads as a credential field; the model never types into one.")
    _, labels = DlpFilter().redact(text)
    if labels:
        raise AppDriverError("secret", f"the text matches the secret scrubber ({', '.join(labels)}); it will not be typed.")
    if looks_like_password(text):
        raise AppDriverError("secret", "the text looks like a password or key (one unbroken token mixing letters, digits and symbols); it will not be typed.")


def scrub(text: str | None) -> str | None:
    """DLP-redact text read from an app before it reaches the model."""
    if text is None:
        return None
    from dourmouse.governance import DlpFilter

    return DlpFilter().redact(text)[0]
