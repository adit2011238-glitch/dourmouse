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

Three real detection rules now, plus a real correlation rule on top: a disabled Application Firewall (HIGH),
any listening service exposed beyond LOOPBACK_ONLY/LOCAL_NETWORK/
TAILSCALE's own expected tiers -- i.e. ALL_INTERFACES (MED) -- and, closed
2026-09-21 (Phase 2 step 1, harsh acceptance test 2), a real persisted
known-device baseline: a real ARP neighbor never seen before is itself a
MED finding, same detection/scoring/memory pipeline every other finding
already uses, no new machinery. The FIRST scan against an empty baseline
seeds it silently rather than flagging every device on the LAN as "new"
-- a real, deliberate choice (see `_detect_findings`'s own
`known_device_keys=None` convention below), not an oversight. External-IP
reputation lookups still need real, separate follow-on work (Phase 2 step
2) -- not built here.

``SentryRuntime`` (2026-09-20, user-directed: "this needs to be a really
powerful cybersecurity system... always running sentry, continuous data
stream") closes the "continuously-running background scheduler" gap named
above: the exact same real daemon-thread shape ``GoalRuntime``/
``SchedulerRunner`` already use (one instance per process, started at
server boot via ``webui.run_server``, a broken tick never kills the loop --
see ``GoalRuntime._loop``'s own identical `try/except: pass` +
`threading.Event.wait` shape, copied here rather than re-derived). Default
interval is 5 minutes, not faster: a real scan shells out to `lsof`/
`ifconfig`/`scutil`/`arp`/`socketfilterfw` every tick, and a real host
security tool polling every few minutes (not every second) matches how
real endpoint security agents actually behave, not a marketing-driven
"real-time" claim this pass cannot back up with real, cheap telemetry.

Still deliberately NOT built in this pass: the live SSE push through
DesktopNotifier (needs the running server's own hub instance, unreachable
from a plain tool call -- checked directly before writing this: no global
accessor for it exists today). A scan today is real, continuous, and
chat-reachable, and a genuinely new HIGH finding writes a real, persisted
alert via state_store.add_alert -- the exact same real mechanism
goal_runtime.py's own system alerts already use -- visible on the next
alerts-screen refresh, just not an instant push.

**Incident/case tracking** (2026-09-21, Phase 2 step 3): `SentryStore`
gains a real `incidents` table -- `OPEN -> INVESTIGATING ->
RESOLVED/ACCEPTED_RISK`, mirroring `goals.py`'s own "terminal states never
silently reopen" discipline (`GOAL_TERMINAL_STATES`) rather than inventing
new status semantics. An incident references a real, already-detected
finding by fingerprint (an unknown fingerprint is a real, honest refusal,
never a silently-created orphan case); a RESOLVED/ACCEPTED_RISK incident
refuses to transition to any OTHER status (a real analyst opens a NEW
incident for a genuine recurrence instead) but CAN still receive a
note-only update or be re-set to the SAME terminal status (a closing
confirmation, not a reopen). A real SOC operator workflow -- triage, note,
close -- on top of what was previously just a flat findings table.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dourmouse.config import workspace_dir
from dourmouse.security import platform_adapter as pa

_DEFAULT_SCAN_INTERVAL_SECONDS = 300.0

def default_db() -> Path:
    """Resolved on every call, never at import time (finding #084): an
    import-time constant froze whatever DOURMOUSE_WORKSPACE was when the
    module was first imported, which let the test suite write into the
    real workspace."""
    return workspace_dir() / "security" / "sentry.db"

#: Real incident lifecycle (Phase 2 step 3) -- mirrors goals.py's own
#: "terminal states never silently reopen" discipline (GOAL_TERMINAL_
#: STATES) rather than inventing new status semantics: RESOLVED and
#: ACCEPTED_RISK are terminal, a genuine operator workflow (triage, note,
#: close), never just a flat findings table.
INCIDENT_STATES = frozenset({"OPEN", "INVESTIGATING", "RESOLVED", "ACCEPTED_RISK"})
INCIDENT_TERMINAL_STATES = frozenset({"RESOLVED", "ACCEPTED_RISK"})

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
CREATE TABLE IF NOT EXISTS known_devices (
    device_key TEXT PRIMARY KEY,
    mac        TEXT,
    ip         TEXT NOT NULL,
    hostname   TEXT,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '[]',
    opened_at   REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline (
    scope      TEXT NOT NULL,
    category   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    times_seen INTEGER NOT NULL,
    PRIMARY KEY (scope, category, key)
);
CREATE TABLE IF NOT EXISTS downloads (
    sha256     TEXT NOT NULL,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    risk       TEXT NOT NULL,
    assessment TEXT NOT NULL,
    seen_at    REAL NOT NULL,
    PRIMARY KEY (sha256, path)
);
CREATE TABLE IF NOT EXISTS baseline_meta (
    scope      TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    scans      INTEGER NOT NULL
);
"""


def _device_key(mac: str | None, ip: str) -> str:
    """A real ARP neighbor's stable identity for the baseline: its MAC
    when the kernel resolved one, else `ip:<address>` -- a real, honest
    fallback for the "(incomplete)" ARP entries platform_adapter's own
    parser already reports as `mac=None`, not a fabricated MAC. Known,
    named limitation: DHCP churn on an unresolved-MAC device changes its
    IP and therefore its key, so it can re-report as "new" -- a real ARP
    limitation, not a bug in this function."""
    return mac if mac else f"ip:{ip}"


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


def _detect_findings(
    state: dict[str, Any], known_device_keys: set[str] | None = None
) -> list[SentryFinding]:
    """Pure, deterministic: the same telemetry snapshot (and the same real
    baseline) always produces the same findings. No I/O, no clock, no
    model -- mirrors this codebase's own established pure-logic-first
    convention (research_mesh/core.py, exams.py's own citation gate). The
    real baseline read/write itself happens in run_scan(), outside this
    function -- `known_device_keys` arrives as a plain snapshot of
    already-known device identities, never a live store handle.

    `known_device_keys=None` (the default, and every pre-existing call
    site) means "no real baseline available for this call" -- the
    new-device rule is skipped entirely rather than comparing against
    nothing, which would flag every real device as new. `run_scan()`
    itself only ever passes `None` on a genuinely empty (first-ever)
    baseline -- see its own docstring."""
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
            recommended_action=(
                "Enable it via System Settings -> Network -> Firewall, or run this "
                "exact real command yourself: "
                "sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on "
                "-- never applied automatically, this is a suggestion only (Phase 2 step 5)."
            ),
        ))

    lp = state.get("listening_ports") or {}
    if lp.get("available"):
        # A service usually listens on an IPv4 and an IPv6 socket for the same
        # port; that is one exposure, not two (finding #110, seen live as
        # "rapportd listens on all interfaces" twice).
        seen_ports: set[tuple[str, int, str]] = set()
        for port in lp["listening_ports"]:
            if port["exposure"] != "ALL_INTERFACES":
                continue
            port_key = (port["command"], port["port"], port["protocol"])
            if port_key in seen_ports:
                continue
            seen_ports.add(port_key)
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
                    f"If {port['command']} does not need LAN/remote access, reconfigure it "
                    f"to bind to 127.0.0.1 instead of {port['bind_address']}, or add this "
                    f"exact real pf rule yourself (in /etc/pf.conf, then "
                    f"`sudo pfctl -f /etc/pf.conf`): "
                    f"block in on en0 proto {port['protocol'].lower()} from any to any "
                    f"port {port['port']} -- never applied automatically, this is a "
                    "suggestion only (Phase 2 step 5)."
                ),
            ))

    arp = state.get("arp_neighbors") or {}
    if arp.get("available") and known_device_keys is not None:
        for neighbor in arp["neighbors"]:
            key = _device_key(neighbor.get("mac"), neighbor["ip"])
            if key in known_device_keys:
                continue
            label = neighbor.get("hostname") or neighbor["ip"]
            findings.append(SentryFinding(
                fingerprint=_fingerprint("new_device", key),
                kind="new_device",
                severity="med",
                title=f"New device joined the network: {label}",
                detail=(
                    f"MAC {neighbor.get('mac') or 'unknown'}, IP {neighbor['ip']} was not "
                    "in this host's previously known device baseline."
                ),
                recommended_action=(
                    f"If you do not recognize this device, block it at your router's admin "
                    f"page (by MAC {neighbor.get('mac') or 'unknown, use IP ' + neighbor['ip']}), "
                    "or investigate it directly first. Never blocked automatically -- this is "
                    "a suggestion only (Phase 2 step 5)."
                ),
            ))

    return findings


def _detect_correlations(new_findings: list[SentryFinding]) -> list[SentryFinding]:
    """Real correlation over THIS scan's own genuinely new findings (Phase
    2 step 4): a real SOC's own value-add over isolated point checks is
    noticing multiple weak signals together, not a vague "AI notices
    patterns" claim -- one explicit, deterministic rule this pass: a new
    LAN device AND a newly-exposed service appearing in the SAME scan (the
    spec's own named example verbatim). Deliberately checks `new_findings`
    (not the persisted, still-open condition list) -- the correlation is
    about a same-window COINCIDENCE, so it fires exactly once, on the scan
    where both first appear together; a scan where either condition was
    already known from a prior scan correctly does not re-fire, since
    `new_findings` never contains an already-known finding. Never
    persisted through `SentryStore.record_and_classify` (a stable
    fingerprint for a coincidence has nothing meaningful to deduplicate
    against on a later, unrelated scan)."""
    kinds = {f.kind for f in new_findings}
    if "new_device" not in kinds or "exposed_port" not in kinds:
        return []
    device = next(f for f in new_findings if f.kind == "new_device")
    port = next(f for f in new_findings if f.kind == "exposed_port")
    return [SentryFinding(
        fingerprint=_fingerprint("correlation", device.fingerprint, port.fingerprint),
        kind="correlated_new_device_and_exposed_port",
        severity="high",
        title="New device and a newly-exposed service appeared in the same scan",
        detail=(
            f"{device.title} AND {port.title} were BOTH first detected in this same scan -- "
            "individually each is a real, separate finding, but the same-window coincidence "
            "raises the real risk this is a coordinated event, not two unrelated background "
            "changes."
        ),
        recommended_action=(
            f"Investigate both together before trusting either: {device.recommended_action} "
            f"{port.recommended_action}"
        ),
    )]


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

    def get_known_device_keys(self) -> set[str]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT device_key FROM known_devices").fetchall()
        return {r[0] for r in rows}

    def record_devices(self, neighbors: list[dict[str, Any]], now: float) -> None:
        """Real upsert of every real ARP neighbor from this scan --
        establishes the baseline on the very first call, refreshes
        last_seen/ip/hostname on every call after."""
        with self._lock, self._conn() as conn:
            for neighbor in neighbors:
                key = _device_key(neighbor.get("mac"), neighbor["ip"])
                row = conn.execute(
                    "SELECT 1 FROM known_devices WHERE device_key=?", (key,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO known_devices "
                        "(device_key, mac, ip, hostname, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (key, neighbor.get("mac"), neighbor["ip"], neighbor.get("hostname"), now, now),
                    )
                else:
                    conn.execute(
                        # COALESCE: a sighting without a name (arp -an never
                        # has one) must not erase a name recorded earlier.
                        "UPDATE known_devices SET ip=?, hostname=COALESCE(?, hostname), "
                        "last_seen=? WHERE device_key=?",
                        (neighbor["ip"], neighbor.get("hostname"), now, key),
                    )
            conn.commit()

    def devices_snapshot(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT device_key, mac, ip, hostname, first_seen, last_seen "
                "FROM known_devices ORDER BY last_seen DESC"
            ).fetchall()
        cols = ["device_key", "mac", "ip", "hostname", "first_seen", "last_seen"]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def open_incident(self, fingerprint: str, note: str, now: float) -> str:
        """Real, idempotent case open. Returns "opened", "already_open"
        (never silently re-creates a real case), or "unknown_fingerprint"
        (a real, honest refusal -- an incident must reference a real,
        already-detected finding, never an arbitrary string)."""
        with self._lock, self._conn() as conn:
            known = conn.execute(
                "SELECT 1 FROM seen_findings WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if known is None:
                return "unknown_fingerprint"
            existing = conn.execute(
                "SELECT 1 FROM incidents WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                return "already_open"
            notes = [{"at": now, "text": note}] if note else []
            conn.execute(
                "INSERT INTO incidents (fingerprint, status, notes, opened_at, updated_at) "
                "VALUES (?, 'OPEN', ?, ?, ?)",
                (fingerprint, json.dumps(notes), now, now),
            )
            conn.commit()
            return "opened"

    def update_incident(
        self, fingerprint: str, status: str | None, note: str | None, now: float
    ) -> str:
        """Real case transition/note. Returns "updated", "not_found", or
        "terminal" (a genuine, honest refusal -- RESOLVED/ACCEPTED_RISK
        never silently reopen; a real analyst must open a NEW incident for
        a recurrence, same "never quietly resurrect a closed record"
        discipline as `goals.py`'s own terminal states)."""
        if status is not None and status not in INCIDENT_STATES:
            raise ValueError(f"unknown incident status: {status!r}")
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT status, notes FROM incidents WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if row is None:
                return "not_found"
            current_status, notes_json = row
            if current_status in INCIDENT_TERMINAL_STATES and status is not None and status != current_status:
                return "terminal"
            notes = json.loads(notes_json)
            if note:
                notes.append({"at": now, "text": note})
            conn.execute(
                "UPDATE incidents SET status=?, notes=?, updated_at=? WHERE fingerprint=?",
                (status or current_status, json.dumps(notes), now, fingerprint),
            )
            conn.commit()
            return "updated"

    def get_incident(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT fingerprint, status, notes, opened_at, updated_at FROM incidents "
                "WHERE fingerprint=?", (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return {
            "fingerprint": row[0], "status": row[1], "notes": json.loads(row[2]),
            "opened_at": row[3], "updated_at": row[4],
        }

    def list_incidents(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT fingerprint, status, notes, opened_at, updated_at FROM incidents "
                    "ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT fingerprint, status, notes, opened_at, updated_at FROM incidents "
                    "WHERE status=? ORDER BY updated_at DESC", (status,),
                ).fetchall()
        return [
            {"fingerprint": r[0], "status": r[1], "notes": json.loads(r[2]),
             "opened_at": r[3], "updated_at": r[4]}
            for r in rows
        ]

    # -- baseline (MS-2, finding #100) ------------------------------------ #

    def load_baseline(self, scopes: set[str]) -> dict[tuple[str, str, str], str]:
        marks = ",".join("?" * len(scopes))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT scope, category, key, value FROM baseline WHERE scope IN ({marks})",  # noqa: S608 -- placeholders only
                tuple(scopes),
            ).fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def baseline_meta(self, scope: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT first_seen, last_seen, scans FROM baseline_meta WHERE scope=?", (scope,)
            ).fetchone()
        return {"first_seen": row[0], "last_seen": row[1], "scans": row[2]} if row else None

    def update_baseline(self, observations: list[Any], scopes: set[str], now: float) -> None:
        """Record this scan's observations and count the scan for each scope
        (a scope with nothing observed still counts: an empty Mac is a real
        baseline). Values are overwritten with the latest; the change itself
        was already reported as an anomaly before this runs."""
        with self._conn() as conn:
            for o in observations:
                conn.execute(
                    "INSERT INTO baseline (scope, category, key, value, first_seen, last_seen, times_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(scope, category, key) DO UPDATE SET "
                    "value=excluded.value, last_seen=excluded.last_seen, times_seen=times_seen+1",
                    (o.scope, o.category, o.key, o.value, now, now),
                )
            for scope in scopes:
                conn.execute(
                    "INSERT INTO baseline_meta (scope, first_seen, last_seen, scans) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(scope) DO UPDATE SET last_seen=excluded.last_seen, scans=scans+1",
                    (scope, now, now),
                )
            conn.commit()

    # -- downloads (MS-4, finding #101) ----------------------------------- #

    def record_download(self, assessment: dict[str, Any], now: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloads (sha256, path, name, kind, risk, assessment, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (assessment["sha256"], assessment["path"], assessment["name"], assessment["kind"],
                 assessment["risk"], json.dumps(assessment), now),
            )
            conn.commit()

    def recent_downloads(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT assessment, seen_at FROM downloads ORDER BY seen_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**json.loads(r[0]), "seen_at": r[1]} for r in rows]

    def forget_baseline_item(self, scope: str, category: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM baseline WHERE scope=? AND category=? AND key=?", (scope, category, key))
            conn.commit()

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


def _write_alert(finding: SentryFinding) -> bool:
    """Real, best-effort alert write -- shared by the ordinary HIGH-
    finding path and the correlation path so both go through the exact
    same real mechanism (`state_store.default_store().add_alert`).
    Returns whether a real alert genuinely landed; never raises, matching
    this scan's own "an observer must never break a real scan" rule."""
    try:
        from dourmouse.state_store import default_store

        default_store().add_alert(
            kind="security",
            title=f"Security: {finding.title}"[:160],
            detail=finding.detail[:400],
            severity="high",
            link="#/security",
        )
        return True
    except Exception:  # noqa: BLE001 -- an observer must never break a real scan
        return False


def collect_state() -> dict[str, Any]:
    """Everything one scan looks at: the network basics from platform_adapter
    plus the Mac telemetry (MS-1): Wi-Fi, host protections, persistence, and
    who each network-active process really is (path, parent, signature)."""
    from . import mac_telemetry as mt

    state = pa.get_system_security_state()
    state["wifi"] = mt.get_wifi()
    state["host_protections"] = mt.get_host_protections()
    state["persistence"] = mt.get_persistence_items()
    est = state.get("established_connections") or {}
    procs = [mt.process_details(int(pid)) for pid in sorted({c.get("pid") for c in est.get("connections", []) if c.get("pid")})]
    # Signature checks run concurrently: spctl takes about 2 s per program
    # cold (it can consult Apple's notarization service), 15 s for 8 programs
    # measured serially on this Mac; results are cached by path, mtime, size.
    from concurrent.futures import ThreadPoolExecutor

    exes = sorted({d["exe"] for d in procs if d.get("available") and d.get("exe")})
    with ThreadPoolExecutor(max_workers=8) as pool:
        sigs = dict(zip(exes, pool.map(mt.code_signature, exes), strict=True))
    for d in procs:
        if d.get("exe") in sigs:
            d["signature"] = sigs[d["exe"]]
    state["network_processes"] = procs
    return state


def run_scan(
    state_fn: Callable[[], dict[str, Any]] = collect_state,
    store: SentryStore | None = None,
    now: Callable[[], float] = time.time,
    write_alerts: bool = True,
) -> SentryScanResult:
    """INITIAL_ASSESSMENT (trivial, this call itself) ->
    INTELLIGENCE_GATHERING (state_fn) -> RISK_ANALYSIS (_detect_findings +
    the real weighted formula + _detect_correlations over this scan's own
    genuinely new findings, Phase 2 step 4) -> ACTION_PLANNING/REPORTING
    (each SentryFinding's own recommended_action, plus a real alert for a
    genuinely new HIGH finding or a real correlation) -> MEMORY_UPDATE
    (SentryStore, read back on every future call automatically via
    record_and_classify, plus the real known-device baseline read here and
    written back at the end).

    The known-device baseline read happens BEFORE `_detect_findings` and
    the write happens AFTER -- this scan's own newly-seen devices must
    never suppress themselves. A genuinely empty baseline (nothing ever
    recorded) passes `known_device_keys=None` into `_detect_findings`, so
    the very first scan ever run seeds the baseline silently instead of
    reporting every device already on the LAN as "new" -- see that
    function's own docstring for why."""
    store = store or SentryStore(default_db())
    state = state_fn()
    known_keys_before = store.get_known_device_keys()
    raw_findings = _detect_findings(
        state, known_device_keys=(known_keys_before or None)
    )

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
            if write_alerts and finding.severity == "high" and _write_alert(finding):
                alerts_written += 1

    for correlation in _detect_correlations(new_findings):
        all_findings.append(correlation)
        new_findings.append(correlation)
        risk_score += _SEVERITY_WEIGHT.get(correlation.severity, 0.0)
        if write_alerts and _write_alert(correlation):
            alerts_written += 1

    # MS-2/MS-3 (finding #100): compare with the baseline, then learn from
    # this scan. Anomalies from a scope still in its learning period are
    # dropped, so a fresh install or a new network seeds silently.
    from . import baseline as bl
    from .mac_detectors import detect_mac_findings
    from .mac_telemetry import network_id

    gw_ip = (state.get("default_gateway") or {}).get("gateway")
    domains = sorted({d for r in (state.get("dns") or {}).get("resolvers", []) for d in r.get("search_domains", [])})
    net = network_id(gw_ip, state.get("wifi") or {}, domains)
    observations = bl.observations(state, net)
    scopes = {bl.HOST, net}
    known = store.load_baseline(scopes)
    learning = {sc for sc in scopes if bl.is_learning(store.baseline_meta(sc), ts)}
    anomalies = [a for a in bl.compare(observations, known) if a.observation.scope not in learning]
    for finding in detect_mac_findings(state, anomalies):
        status = store.record_and_classify(finding, ts)
        if status == "dismissed":
            suppressed.append(finding)
            continue
        all_findings.append(finding)
        risk_score += _SEVERITY_WEIGHT.get(finding.severity, 0.0)
        if status == "new":
            new_findings.append(finding)
            if write_alerts and finding.severity == "high" and _write_alert(finding):
                alerts_written += 1
    store.update_baseline(observations, scopes, ts)

    arp = state.get("arp_neighbors") or {}
    if arp.get("available"):
        store.record_devices(arp["neighbors"], ts)

    base_keys = ("interfaces", "default_gateway", "dns", "arp_neighbors", "listening_ports", "firewall")
    telemetry_available: dict[str, bool] = {
        key: bool((state.get(key) or {}).get("available"))
        for key in base_keys + ("wifi", "host_protections", "persistence")
        if key in state or key in base_keys
    }
    return SentryScanResult(
        all_findings=all_findings,
        new_findings=new_findings,
        suppressed_false_positives=suppressed,
        risk_score=risk_score,
        telemetry_available=telemetry_available,
        alerts_written=alerts_written,
    )


def sentry_runtime_enabled() -> bool:
    """Default ON, same opt-OUT convention as goal_runtime_enabled() --
    set DOURMOUSE_SECURITY_SENTRY_LOOP=0 to disable the continuous scan."""
    return os.environ.get("DOURMOUSE_SECURITY_SENTRY_LOOP", "1").strip() != "0"


class SentryRuntime:
    """Ticks a real security scan on a real interval -- one instance per
    process, started as a daemon thread from webui.run_server exactly like
    GoalRuntime/SchedulerRunner. A broken tick logs nothing special and
    never kills the loop; the next tick tries again."""

    def __init__(
        self,
        interval_seconds: float = _DEFAULT_SCAN_INTERVAL_SECONDS,
        store: SentryStore | None = None,
        state_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        # Never faster than once a minute -- a real scan shells out to
        # several real system commands per tick, not a free in-memory check.
        self._interval = max(60.0, float(interval_seconds))
        self._store = store or SentryStore(default_db())
        # None: the real Mac telemetry (collect_state). Injected in tests of
        # the loop itself, which must not depend on a real scan's cost (a
        # cold scan with signature checks takes about 10 s on this Mac).
        self._state_fn = state_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The loop and the network watcher (MS-10) can both ask for a scan;
        # one runs at a time, and each result goes to every listener.
        self._scan_lock = threading.Lock()
        self._listeners: list[Callable[[SentryScanResult, str], None]] = []
        self.last_result: SentryScanResult | None = None
        self.last_scan_at: float | None = None
        self.tick_count = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="dourmouse-security-sentry"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def add_listener(self, fn: Callable[[SentryScanResult, str], None]) -> None:
        self._listeners.append(fn)

    def run_one_tick_now(self, reason: str = "scheduled") -> SentryScanResult:
        """Real, synchronous, out-of-band scan -- used by the chat tool so
        a user asking "scan now" does not have to wait for the next
        scheduled tick, by the network watcher on a network change, and by
        tests that want a real tick without a real threading.Event.wait
        delay."""
        with self._scan_lock:
            result = run_scan(state_fn=self._state_fn or collect_state, store=self._store)
            self.last_result = result
            self.last_scan_at = time.time()
            self.tick_count += 1
        for fn in list(self._listeners):
            with contextlib.suppress(Exception):  # a listener's bug never breaks the scan
                fn(result, reason)
        return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_one_tick_now()
            except Exception:  # noqa: BLE001 -- a bug in one tick must never kill the runtime
                pass
            self._stop.wait(self._interval)
