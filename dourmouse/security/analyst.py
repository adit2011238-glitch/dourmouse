"""The AI security analyst (MS-9, spec items 34-37; finding #107).

The detectors decide what is wrong; they are deterministic and never call a
model. The analyst wakes when they find something new, and its only job is
to explain: what the findings mean together, how worried to be, and what to
do first, in plain English. It is held to that:

- it sees only the findings' own evidence, and every point it makes must
  cite one of them by number; a point that cites nothing, or a number that
  was not given, is dropped (it cannot invent a finding);
- it never acts: suggested next steps are text, and any action goes through
  the approval-gated response tools like everything else;
- it uses a large cloud model (the owner's model policy), and when that is
  unavailable it says so instead of producing a fake analysis.

Wake-ups are rate-limited and deduplicated, so a noisy network costs one
analysis, not one per scan.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

Complete = Callable[[list[dict[str, str]]], str]

SYSTEM = (
    "You are the security analyst inside Dourmouse, a Mac assistant. Deterministic detectors have already "
    "found the numbered findings below on the owner's own Mac; each has its evidence. Explain them to the "
    "owner in plain English. Rules: use only the evidence given; do not claim anything the evidence does not "
    "show; every point must cite the finding numbers it is about; say plainly when something is probably "
    "harmless; never tell the owner to paste commands they do not understand. Answer with ONLY a JSON object: "
    '{"summary": "two or three sentences", "worry": "none|low|medium|high", '
    '"points": [{"findings": [1], "meaning": "what this means", "first_step": "what to do first"}]}'
)
WORRIES = ("none", "low", "medium", "high")
MIN_INTERVAL = 600.0
WAKE_SEVERITIES = ("high", "med")


def analyst_dir() -> Path:
    return workspace_dir() / "security" / "analyst"


def default_complete() -> Complete:
    """A large cloud model, never local (the owner's model policy)."""
    from dourmouse.dispatch import OllamaNativeClient, _ollama_cloud_config

    cfg = _ollama_cloud_config()
    client = OllamaNativeClient(cfg)

    def complete(messages: list[dict[str, str]]) -> str:
        # No max_tokens: this backend reasons in `content` before it
        # answers, and a cap cuts the answer off (see the brevity finding).
        resp = client.chat.completions.create(model=cfg.model, messages=messages)
        return str(resp.choices[0].message.content or "")

    return complete


def _extract_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    while start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            start = text.find("{", start + 1)
    return None


def evidence_block(findings: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[{i}] ({f['severity']}) {f['title']}\nEvidence: {f['detail']}\nSuggested fix: {f.get('recommended_action', '')}"
        for i, f in enumerate(findings, 1))


def analyze(findings: list[dict[str, Any]], complete: Complete | None = None) -> dict[str, Any]:
    if not findings:
        return {"ok": True, "summary": "Nothing to analyze: no findings.", "worry": "none", "points": [], "dropped": 0}
    if complete is None:
        from .privacy import privacy_mode

        if privacy_mode():  # finding #112: the default model is in the cloud
            return {"ok": False, "error": "privacy mode is on: findings are not sent to the cloud model"}
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Findings:\n\n" + evidence_block(findings)}]
    try:
        raw = (complete or default_complete())(messages)
    except Exception as exc:  # noqa: BLE001 -- any backend failure is reported, never papered over
        return {"ok": False, "error": f"the analyst model is unavailable: {exc}"}
    parsed = _extract_json(raw)
    if parsed is None or not isinstance(parsed.get("summary"), str):
        return {"ok": False, "error": "the analyst model did not return an analysis", "raw": raw[:2000]}
    valid = set(range(1, len(findings) + 1))
    points, dropped = [], 0
    for p in parsed.get("points") or []:
        refs = [r for r in (p.get("findings") or []) if isinstance(r, int) and not isinstance(r, bool)] \
            if isinstance(p, dict) else []
        if not refs or not set(refs) <= valid or not isinstance(p.get("meaning"), str):
            dropped += 1
            continue
        points.append({"findings": refs, "titles": [findings[r - 1]["title"] for r in refs],
                       "meaning": p["meaning"], "first_step": str(p.get("first_step") or "")})
    worry = parsed.get("worry") if parsed.get("worry") in WORRIES else "unknown"
    return {"ok": True, "summary": parsed["summary"], "worry": worry, "points": points, "dropped": dropped}


class Analyst:
    """Wakes on new findings of medium or high severity; at most one
    analysis per MIN_INTERVAL, and never twice for the same set."""

    def __init__(self, complete: Complete | None = None, notify: Callable[[str], None] | None = None,
                 folder: Path | None = None, min_interval: float = MIN_INTERVAL) -> None:
        self._complete = complete
        self._notify = notify
        self._folder = folder
        self._min_interval = min_interval
        self._last_at = 0.0
        self._last_set: frozenset[str] = frozenset()
        self._lock = threading.Lock()
        self.last: dict[str, Any] | None = None

    def should_wake(self, new_findings: list[Any], now: float) -> bool:
        wanted = frozenset(f.fingerprint for f in new_findings if f.severity in WAKE_SEVERITIES)
        if not wanted or wanted <= self._last_set:
            return False
        return now - self._last_at >= self._min_interval

    def on_scan(self, result: Any, now: float | None = None) -> dict[str, Any] | None:
        now = now if now is not None else time.time()
        with self._lock:
            if not self.should_wake(result.new_findings, now):
                return None
            self._last_at = now
            self._last_set = frozenset(f.fingerprint for f in result.new_findings if f.severity in WAKE_SEVERITIES)
        findings = [{"kind": f.kind, "severity": f.severity, "title": f.title, "detail": f.detail,
                     "recommended_action": f.recommended_action} for f in result.all_findings]
        out = {**analyze(findings, self._complete), "at": now,
               "woke_for": [f.title for f in result.new_findings if f.severity in WAKE_SEVERITIES]}
        self.last = out
        from .privacy import private_dir

        folder = private_dir(self._folder or analyst_dir())
        (folder / f"analysis-{int(now)}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        if self._notify and out.get("ok"):
            self._notify(f"Security: {out['summary'][:180]}")
        return out


def latest(folder: Path | None = None) -> dict[str, Any] | None:
    folder = folder or analyst_dir()
    files = sorted(folder.glob("analysis-*.json")) if folder.is_dir() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
