"""AI security sentry (Domain I remainder): a real, deterministic scan over
platform_adapter's own real telemetry, scored against a real weighted
formula, with real persisted history so a dismissed false positive stays
dismissed on the next scan (ThreatSentinel-adapted architecture, see
docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md Domain I).

Deliberately NO model call in this pass: a security finding's own severity
and existence must be exactly reproducible, not a paraphrase a model could
drop a detail from. This corrects this domain's own build plan, which
first proposed "one real ChatSession call" for scoring -- corrected here,
before writing any detection rule, in favor of a real, explicit, auditable
formula, matching this domain's own explicit requirement ("never a bare
LLM vibe-check with no formula behind it").

Two real detection rules this pass, named honestly as a start, not a
finished detector: a disabled Application Firewall (HIGH), and any
listening service exposed beyond LOOPBACK_ONLY/LOCAL_NETWORK/TAILSCALE's
own expected tiers -- i.e. ALL_INTERFACES (MED). New-LAN-device detection
(this domain's own harsh acceptance test 2) and external-IP reputation
lookups need a real, persisted ARP-neighbor baseline this pass does not
yet build -- named explicitly as real, separate, not-yet-done follow-on.

Also deliberately NOT built in this pass: a continuously-running
background scheduler wired into webui.py's own startup (needed for this
domain's own harsh acceptance test 1's "unprompted, within a bounded
window" wording) and the live SSE push through DesktopNotifier (needs the
running server's own hub instance, unreachable from a plain tool call --
checked directly before writing this: no global accessor for it exists
today). A scan today is real and chat-reachable, and a genuinely new HIGH
finding writes a real, persisted alert via state_store.add_alert -- the
exact same real mechanism goal_runtime.py's own system alerts already use
-- visible on the next alerts-screen refresh, just not an instant push.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dourmouse.config import workspace_dir
from dourmouse.security import platform_adapter as pa

DEFAULT_DB = workspace_dir() / "security" / "sentry.db"

# Real, explicit, auditable weights -- adapted from ThreatSentinel's own
# "weighted base severity" component. Historical pattern match (the other
# half of ThreatSentinel's formula) is real too: a dismissed false positive
# is excluded from this sum entirely, not just down-weighted.
_SEVERITY_WEIGHT = {"low": 1.0, "med": 4.0, "high": 9.0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_findings (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    times_seen  INTEGER NOT NULL,
    dismissed_false_positive INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class SentryFinding:
    """One real, deterministically-detected condition. `fingerprint` is
    stable across scans for the SAME underlying condition (e.g. the same
    firewall check, the same command+port pair) so repeat detection and
    false-positive dismissal both work correctly."""

    fingerprint: str
    kind: str
    severity: str  # "low" | "med" | "high" -- matches state_store.SEVERITIES
    title: str
    detail: str
    recommended_action: str


@dataclass
class SentryScanResult:
    all_findings: list[SentryFinding]
    new_findings: list[SentryFinding]  # genuinely first-ever detection this scan
    suppressed_false_positives: list[SentryFinding]
    risk_score: float
    telemetry_available: dict[str, bool]
    alerts_written: int


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _detect_findings(state: dict[str, Any]) -> list[SentryFinding]:
    """Pure, deterministic: the same telemetry snapshot always produces the
    same findings. No I/O, no clock, no model -- mirrors this codebase's
    own established pure-logic-first convention (research_mesh/core.py,
    exams.py's own citation gate)."""
    findings: list[SentryFinding] = []

    fw = state.get("firewall") or {}
    if fw.get("available") and not fw.get("enabled"):
        findings.append(SentryFinding(
            fingerprint=_fingerprint("firewall_disabled"),
            kind="firewall_disabled",
            severity="high",
            title="Application Firewall is disabled",
            detail="macOS Application Firewall is currently OFF -- every listening "
                   "service on this machine accepts unsolicited inbound connections.",
            recommended_action="Enable it: System Settings -> Network -> Firewall. "
                                "Not applied automatically -- this is a suggestion, "
                                "never a silent change.",
        ))

    lp = state.get("listening_ports") or {}
    if lp.get("available"):
        for port in lp["listening_ports"]:
            if port["exposure"] != "ALL_INTERFACES":
                continue
            findings.append(SentryFinding(
                fingerprint=_fingerprint(
                    "exposed_port", port["command"], str(port["port"]), port["protocol"]
                ),
                kind="exposed_port",
                severity="med",
                title=f"{port['command']} listens on all interfaces",
                detail=(
                    f"{port['command']} (pid {port['pid']}) is reachable on "
                    f"{port['protocol']} port {port['port']} from any network this "
                    "host is on, not just this machine."
                ),
                recommended_action=(
                    "If this service does not need LAN/remote access, bind it to "
                    "127.0.0.1 or restrict it with a firewall rule instead. Not "
                    "applied automatically."
                ),
            ))

    return findings


class SentryStore:
    """Real, resumable history of findings this sentry has ever seen --
    same one-connection-per-operation, WAL-mode SQLite discipline as every
    other real store in this codebase (research_mesh/store.py, goals.py)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=30.0)

    def record_and_classify(
        self, finding: SentryFinding, now: float
    ) -> str:
        """Real read-then-write against the store; returns "new", "known",
        or "dismissed". Never called concurrently for the SAME fingerprint
        within one scan (run_scan calls this sequentially per finding), so
        a single connection per call is enough -- no cross-process race to
        guard against beyond what WAL already gives every writer."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT dismissed_false_positive, times_seen FROM seen_findings WHERE fingerprint=?",
                (finding.fingerprint,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO seen_findings "
                    "(fingerprint, kind, severity, title, first_seen, last_seen, times_seen, "
                    "dismissed_false_positive) VALUES (?, ?, ?, ?, ?, ?, 1, 0)",
                    (finding.fingerprint, finding.kind, finding.severity, finding.title, now, now),
                )
                return "new"
            dismissed, times_seen = row
            conn.execute(
                "UPDATE seen_findings SET last_seen=?, times_seen=? WHERE fingerprint=?",
                (now, times_seen + 1, finding.fingerprint),
            )
            return "dismissed" if dismissed else "known"

    def mark_false_positive(self, fingerprint: str) -> bool:
        """MEMORY_UPDATE's own real mechanism: a user-declined finding stays
        suppressed on every future scan until this is reversed. Returns
        False (honest, not a crash) when the fingerprint was never seen."""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE seen_findings SET dismissed_false_positive=1 WHERE fingerprint=?",
                (fingerprint,),
            )
            conn.commit()
            return cur.rowcount > 0

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT fingerprint, kind, severity, title, first_seen, last_seen, "
                "times_seen, dismissed_false_positive FROM seen_findings "
                "ORDER BY last_seen DESC"
            ).fetchall()
        cols = ["fingerprint", "kind", "severity", "title", "first_seen", "last_seen",
                "times_seen", "dismissed_false_positive"]
        return [dict(zip(cols, r, strict=True)) for r in rows]


def run_scan(
    state_fn: Callable[[], dict[str, Any]] = pa.get_system_security_state,
    store: SentryStore | None = None,
    now: Callable[[], float] = time.time,
    write_alerts: bool = True,
) -> SentryScanResult:
    """INITIAL_ASSESSMENT (trivial, this call itself) ->
    INTELLIGENCE_GATHERING (state_fn) -> RISK_ANALYSIS (_detect_findings +
    the real weighted formula) -> ACTION_PLANNING/REPORTING (each
    SentryFinding's own recommended_action, plus a real alert for a
    genuinely new HIGH finding) -> MEMORY_UPDATE (SentryStore, read back
    on every future call automatically via record_and_classify)."""
    store = store or SentryStore(DEFAULT_DB)
    state = state_fn()
    raw_findings = _detect_findings(state)

    ts = now()
    all_findings: list[SentryFinding] = []
    new_findings: list[SentryFinding] = []
    suppressed: list[SentryFinding] = []
    risk_score = 0.0
    alerts_written = 0

    for finding in raw_findings:
        status = store.record_and_classify(finding, ts)
        if status == "dismissed":
            suppressed.append(finding)
            continue
        all_findings.append(finding)
        risk_score += _SEVERITY_WEIGHT.get(finding.severity, 0.0)
        if status == "new":
            new_findings.append(finding)
            # Only HIGH severity proactively alerts -- MED findings this
            # scan's own rules produce (e.g. Spotify/rapportd on
            # ALL_INTERFACES) are common, legitimate, expected services on
            # a real machine; alerting on every one of them the first time
            # a user ever scans would be noise, not signal. Still reported
            # in the real scan text either way -- never hidden, just not
            # proactively pushed as an alert.
            if write_alerts and finding.severity == "high":
                try:
                    from dourmouse.state_store import default_store

                    default_store().add_alert(
                        kind="system",
                        title=f"Security: {finding.title}"[:160],
                        detail=finding.detail[:400],
                        severity="high",
                        link="#/security",
                    )
                    alerts_written += 1
                except Exception:  # noqa: BLE001 -- an observer must never break a real scan
                    pass

    telemetry_available = {
        key: bool((state.get(key) or {}).get("available"))
        for key in ("interfaces", "default_gateway", "dns", "arp_neighbors",
                    "listening_ports", "firewall")
    }
    return SentryScanResult(
        all_findings=all_findings,
        new_findings=new_findings,
        suppressed_false_positives=suppressed,
        risk_score=risk_score,
        telemetry_available=telemetry_available,
        alerts_written=alerts_written,
    )
