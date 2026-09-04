"""Query the Windows desktop's large spatial vault as a REAL RAG source,
over SSH, with the embedding done ON the desktop.

Why this module exists at all — the blocker ``shared_rag.py`` cannot solve
-------------------------------------------------------------------------
``shared_rag.py`` already knows how to read ``hybrid_vault.db`` +
``vector.index``. It cannot actually USE them from here, and refusing to is
the correct behaviour, not a bug:

- the vault was embedded with ``sentence-transformers/all-MiniLM-L6-v2`` at
  **384** dims (live-proven: ``index.reconstruct()`` at real range
  boundaries matched an independently recomputed MiniLM embedding, cosine
  1.000000 — re-confirmed by this module's own probe, see below);
- Dourmouse's own store embeds with Ollama's ``nomic-embed-text`` at **768**
  dims (``global_memory.EMBED_MODEL`` / ``EMBED_DIM``);
- those are different vector spaces, so ``shared_rag._check_index_dimension``
  refuses the whole path with ``EMBEDDING_MISMATCH``. Comparing across them
  is meaningless, not merely lower-quality.

The only honest fix is to embed the QUERY with the same model that built the
index — and that model, the 347 MB index and the 2.98 GB SQLite file all
live on the desktop. So this module ships a small Python program to the
desktop over SSH, runs embed + FAISS search + row lookup THERE, and brings
back real rows. Nothing is embedded, compared or scored on this machine.

What was verified live on the real desktop (not assumed)
--------------------------------------------------------
Probed directly over SSH before this module was written:

- Python 3.11.2 on PATH as ``python``; ``numpy``, ``faiss``,
  ``sentence_transformers`` and ``torch`` all already importable — nothing
  needed installing, and this module installs nothing (see ``_REMOTE_SCRIPT``:
  it only ever READS).
- ``all-MiniLM-L6-v2`` already present in the desktop's HuggingFace cache, so
  the remote program sets ``HF_HUB_OFFLINE``/``TRANSFORMERS_OFFLINE`` and
  never reaches the network from the user's machine.
- ``vector.index`` is a bare ``IndexFlatL2``: 226,076 vectors, dim 384, and
  every stored vector is unit-norm (``|v| = 1.0000`` at every position
  sampled) — which is what makes the honest cosine score below possible.
- ``hybrid_chunks`` has 1,023,765 rows across three ``source_pipeline``
  values with disjoint id ranges, and the index covers only two of them.

The position-vs-id bug, and why this module does NOT just trust a constant
--------------------------------------------------------------------------
A bare ``IndexFlatL2`` carries NO id map: ``.search()`` returns 0-based
POSITIONS, not SQL ids. ``shared_rag._load_position_id_map`` documents this
in full and solves it with two operator-set overrides. This module reuses
that exact idea and ships the same live-verified values as defaults
(``_DEFAULT_ID_FILTER_SQL`` / ``_DEFAULT_ID_ORDER_SQL``).

But ``shared_rag`` deliberately refused to hardcode them, for a real reason:
they encode today's index shape and **go stale the moment more of the vault
gets embedded**. Shipping them as defaults would reintroduce exactly that
silent-staleness risk — a stale map returns real-looking rows that are
simply the WRONG rows, which is the precise shape of a Rule 2.2 violation.

So the defaults are paired with a per-query PROOF instead of trust: for every
hit, the remote program pulls the index's own stored vector back out with
``index.reconstruct(position)`` and re-embeds the SQL row it mapped that
position to. If the two do not match (cosine >= ``_VERIFY_MIN_COSINE``), the
map is stale and the whole query fails with ``MAPPING_MISMATCH`` — it never
returns the row. Each returned hit carries ``verify_cosine`` as the receipt.
That turns "these constants are right today" into "these rows are proven
right on this query", which survives the vault growing.

Scoring is real, not normalized rank
------------------------------------
``shared_rag.query_spatial_vault`` min-max normalizes within the result set
because it cannot verify the index's metric from here. This module can and
does: it reconstructs each hit's stored vector and takes a true cosine
against the unit-normalized query vector. ``score`` is therefore a genuine
cosine similarity in ``[-1, 1]``, comparable across queries; ``distance`` is
FAISS's own raw squared-L2 output, kept alongside it unaltered.

Transport, timeouts and the two real SSH hang bugs
--------------------------------------------------
``history_sync.py`` traced two live, reproducible hangs on this exact pair of
machines, and both apply here:

1. ``subprocess.run(..., capture_output=True)`` (PIPE stdio) hangs
   unpredictably when spawning ``ssh.exe``/``scp.exe``, for an arbitrary
   duration, regardless of the remote command. Redirecting to real temp FILES
   was reliable every time. ``_run_ssh_command`` below does the same — and
   feeds the remote program in on **stdin from a real file** too, never a
   PIPE, for the same reason.
2. ``find -exec`` hangs over non-interactive SSH on the Mac. Not relevant
   here (this module runs no ``find``), but it is why the remote side is ONE
   ``python -`` invocation and not a shell pipeline.

``GSSAPIAuthentication=no`` is carried over for the same "never hurts" reason.
Every call has a hard timeout and converts an overrun into a real ``TIMEOUT``
error, so a chat turn degrades honestly instead of wedging.

Injection safety
----------------
The user's query is NEVER interpolated into a command string. It goes into a
JSON payload, base64-encoded, checked against a strict alphabet
(``[A-Za-z0-9+/=]``, which contains no ``cmd.exe`` or POSIX shell
metacharacter), and passed as a single argv token that the remote program
decodes inside Python. ``subprocess`` is called with an argv list and never
``shell=True``.

Cost, honestly
--------------
Measured live: a warm query is ~12 s end to end (the ~7 s
``sentence_transformers`` import dominates); a fully cold one was ~76 s,
of which ~38 s was rebuilding the position->id map by sorting a million
rows. The remote program therefore caches that map (int64 little-endian,
~1.8 MB) in the desktop's own temp dir, keyed by the index's ``ntotal`` and
the table's ``MAX(id)`` so any growth invalidates it. That is the only file
this module ever writes on the desktop, and a failure to write it is
swallowed — the query just pays the slow path.

This is NOT a sub-second retrieval surface. ``_DEFAULT_TIMEOUT`` is sized for
a cold run on purpose; callers wanting a fast fail should pass a smaller one.

Configuration (all env-var driven; the SSH target has NO default)
-----------------------------------------------------------------
``DOURMOUSE_DESKTOP_RAG_HOST`` / ``_USER`` / ``_KEY`` must all be set — this
module opens an SSH session to one specific machine, so it is off unless a
human says where and with which dedicated key. Everything else
(vault paths, table/columns, model, the two id-map overrides, timeouts) has a
live-verified default and an override; see the ``_ENV_*`` constants.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

__all__ = [
    "DesktopRagError",
    "desktop_rag_configured",
    "desktop_rag_config",
    "desktop_available",
    "query_desktop_rag",
    "desktop_rag_status",
    "format_desktop_rag",
]

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_ENV_HOST = "DOURMOUSE_DESKTOP_RAG_HOST"          # required to enable
_ENV_USER = "DOURMOUSE_DESKTOP_RAG_USER"          # required to enable
_ENV_KEY = "DOURMOUSE_DESKTOP_RAG_KEY"            # required to enable
_ENV_DB = "DOURMOUSE_DESKTOP_RAG_DB"
_ENV_INDEX = "DOURMOUSE_DESKTOP_RAG_INDEX"
_ENV_TABLE = "DOURMOUSE_DESKTOP_RAG_TABLE"
_ENV_MODEL = "DOURMOUSE_DESKTOP_RAG_MODEL"
_ENV_PYTHON = "DOURMOUSE_DESKTOP_RAG_PYTHON"
_ENV_ID_FILTER = "DOURMOUSE_DESKTOP_RAG_ID_FILTER_SQL"
_ENV_ID_ORDER = "DOURMOUSE_DESKTOP_RAG_ID_ORDER_SQL"
_ENV_TIMEOUT = "DOURMOUSE_DESKTOP_RAG_TIMEOUT"
_ENV_PROBE_TIMEOUT = "DOURMOUSE_DESKTOP_RAG_PROBE_TIMEOUT"

# Live-verified values for the real desktop vault (see module docstring).
# Defaults, not assumptions: every query PROVES the id map still holds via
# reconstruct()+re-embed, and fails with MAPPING_MISMATCH if it no longer does.
_DEFAULT_DB = "D:/spatial_ai_library/hybrid_omnidisciplinary_vault/hybrid_vault.db"
_DEFAULT_INDEX = "D:/spatial_ai_library/hybrid_omnidisciplinary_vault/vector.index"
_DEFAULT_TABLE = "hybrid_chunks"
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_PYTHON = "python"
# The index covers HuggingFace_Parquet_Stream (ids 1..81166) then
# English_Wikipedia (878856..1023765) — 81166 + 144910 == 226076 == ntotal.
# Pristine_Filtered_Stream (797,689 rows) is genuinely NOT embedded.
_DEFAULT_ID_FILTER_SQL = (
    "source_pipeline IN ('HuggingFace_Parquet_Stream', 'English_Wikipedia')"
)
# false/0 sorts before true/1, so every non-Wikipedia row comes first in id
# order, then every Wikipedia row in id order — the real build sequence.
_DEFAULT_ID_ORDER_SQL = "(source_pipeline = 'English_Wikipedia'), id ASC"

_DEFAULT_TIMEOUT = 150  # seconds; sized for a genuinely cold run (see docstring)
_DEFAULT_PROBE_TIMEOUT = 12  # seconds; the dependency/vault probe imports nothing heavy
_AVAILABLE_TIMEOUT = 8  # seconds; the cheap "is it even up?" check
_CONNECT_TIMEOUT = 10  # seconds; ssh -o ConnectTimeout

_SENTINEL = "__DOURMOUSE_DESKTOP_RAG__"
_B64_ALPHABET = re.compile(r"\A[A-Za-z0-9+/=]+\Z")
# A hit whose stored vector doesn't match a fresh embedding of the row it
# mapped to means the position->id map is stale. 0.98 rather than 1.0 only to
# absorb float32/BLAS jitter — real matches measured 1.000000 at every
# position sampled, and a genuinely wrong row scores nowhere near this.
_VERIFY_MIN_COSINE = 0.98


class DesktopRagError(RuntimeError):
    """Honest, typed failure — never a silent empty result and never a
    fabricated hit (Rule 2.2). ``kind`` is one of NOT_CONFIGURED, UNREACHABLE,
    TIMEOUT, MISSING_DEPENDENCY, VAULT_MISSING, MAPPING_MISMATCH, REMOTE_ERROR,
    BAD_RESPONSE, BAD_REQUEST. ``str(err)`` always carries the kind prefixed.
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"{kind}: {message}")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on anything
    unparseable — a typo'd timeout must not crash a chat turn."""
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def desktop_rag_configured() -> bool:
    """Deterministic on/off switch, matching ``shared_rag`` /
    ``history_sync``'s own contract: never inferred, on only when the SSH
    target is genuinely set. There is no default host — this module opens a
    session to one specific machine."""
    return all(_env(n) for n in (_ENV_HOST, _ENV_USER, _ENV_KEY))


def desktop_rag_config() -> dict[str, Any] | None:
    """Resolved config, or None when not configured (honest "not set up"
    rather than a guessed target — same shape of decision as
    ``history_sync.sync_config``). Never contains a secret: ``key`` is a path
    to a private key, never its contents."""
    if not desktop_rag_configured():
        return None
    db = _env(_ENV_DB) or _DEFAULT_DB
    return {
        "host": _env(_ENV_HOST),
        "user": _env(_ENV_USER),
        "key": _env(_ENV_KEY),
        "python": _env(_ENV_PYTHON) or _DEFAULT_PYTHON,
        "db": db,
        "index": _env(_ENV_INDEX) or _DEFAULT_INDEX,
        "table": _env(_ENV_TABLE) or _DEFAULT_TABLE,
        "model": _env(_ENV_MODEL) or _DEFAULT_MODEL,
        "id_filter_sql": _env(_ENV_ID_FILTER) or _DEFAULT_ID_FILTER_SQL,
        "id_order_sql": _env(_ENV_ID_ORDER) or _DEFAULT_ID_ORDER_SQL,
        "timeout": _env_int(_ENV_TIMEOUT, _DEFAULT_TIMEOUT),
        "probe_timeout": _env_int(_ENV_PROBE_TIMEOUT, _DEFAULT_PROBE_TIMEOUT),
    }


# --------------------------------------------------------------------------- #
# Transport (injectable — no test ever touches the network)
# --------------------------------------------------------------------------- #


def _run_ssh_command(
    cmd: list[str], timeout: int, stdin_path: str | None = None
) -> SimpleNamespace:
    """Run one ssh subprocess. Returns ``.returncode`` / ``.stdout`` /
    ``.stderr`` — subprocess.run's shape, so callers don't care about the
    plumbing.

    Deliberately NOT ``capture_output=True``: PIPE-based stdio hangs
    unpredictably when spawning ``ssh.exe`` on this deployment's Windows side
    (traced live — see ``history_sync``'s module docstring and this module's).
    stdout/stderr go to real temp files, and the remote program is fed in from
    a real file on stdin for the same reason. stdin is ALWAYS redirected
    (``os.devnull`` when there is nothing to send) so ssh can never swallow
    the caller's own stdin.

    This is the ONE function tests replace (via the ``runner`` argument on the
    public functions), so the temp-file detail can change again without any
    test knowing about it.
    """
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "stdout.txt"
        err_path = Path(td) / "stderr.txt"
        with open(stdin_path or os.devnull, "rb") as in_f, open(
            out_path, "wb"
        ) as out_f, open(err_path, "wb") as err_f:
            proc = subprocess.run(
                cmd, stdin=in_f, stdout=out_f, stderr=err_f, timeout=timeout
            )
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
    return SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr)


Runner = Callable[..., SimpleNamespace]


def _ssh_base(cfg: dict[str, Any]) -> list[str]:
    return [
        "ssh",
        "-i", cfg["key"],
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        # A genuine (if minor) contributor to the connection variance traced
        # live in history_sync. Never hurts to skip a negotiation this setup
        # does not use.
        "-o", "GSSAPIAuthentication=no",
        "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
        f"{cfg['user']}@{cfg['host']}",
    ]


def _encode_payload(payload: dict[str, Any]) -> str:
    """base64 of the JSON request. The user's query rides in here and is
    decoded inside Python on the far side — it is NEVER interpolated into a
    command string. The alphabet assertion is belt-and-braces: standard
    base64 contains no ``cmd.exe`` or POSIX shell metacharacter, and this
    proves it for the exact bytes about to be sent."""
    blob = base64.b64encode(
        json.dumps(payload, ensure_ascii=True).encode("utf-8")
    ).decode("ascii")
    if not _B64_ALPHABET.match(blob):
        raise DesktopRagError(
            "BAD_REQUEST",
            "refusing to send a payload that is not pure base64 — this should "
            "be impossible and is checked because the argument crosses a "
            "remote shell.",
        )
    return blob


def _parse_sentinel(stdout: str) -> dict[str, Any]:
    """Pull the one machine-readable line out of the remote program's output.

    Necessary rather than "just parse stdout": loading the model prints a
    BertModel LOAD REPORT and a progress bar to stdout, and torch logs
    deprecation warnings — real, unavoidable noise around the real answer.
    """
    for line in stdout.splitlines():
        marker = line.find(_SENTINEL)
        if marker >= 0:
            raw = line[marker + len(_SENTINEL):].strip()
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                raise DesktopRagError(
                    "BAD_RESPONSE",
                    f"the desktop answered on the result channel but it was not "
                    f"JSON ({exc}). Raw: {raw[:300]!r}",
                ) from exc
            if not isinstance(parsed, dict):
                raise DesktopRagError(
                    "BAD_RESPONSE",
                    f"the desktop's result was {type(parsed).__name__}, not an object.",
                )
            return parsed
    raise DesktopRagError(
        "BAD_RESPONSE",
        "the desktop produced no result line. Its Python program either never "
        f"ran or died before answering. Last output: {stdout[-500:]!r}",
    )


def _remote_call(
    cfg: dict[str, Any],
    payload: dict[str, Any],
    timeout: int,
    runner: Runner | None,
) -> dict[str, Any]:
    """Ship ``_REMOTE_SCRIPT`` to the desktop, run it with ``payload``, return
    its parsed answer. Raises ``DesktopRagError`` for every failure mode —
    never returns a partial or invented result."""
    run = runner or _run_ssh_command
    blob = _encode_payload(payload)
    cmd = _ssh_base(cfg) + [cfg["python"], "-", blob]
    with tempfile.TemporaryDirectory() as td:
        script_path = Path(td) / "desktop_rag_remote.py"
        script_path.write_text(_REMOTE_SCRIPT, encoding="utf-8")
        try:
            result = run(cmd, timeout=timeout, stdin_path=str(script_path))
        except subprocess.TimeoutExpired as exc:
            raise DesktopRagError(
                "TIMEOUT",
                f"the desktop did not answer within {timeout}s. Nothing was "
                "returned and nothing was guessed. A genuinely cold run (OS "
                "file cache empty) has been measured at ~76s; raise "
                f"{_ENV_TIMEOUT} if that is expected here.",
            ) from exc
        except OSError as exc:
            raise DesktopRagError(
                "UNREACHABLE", f"could not start ssh: {exc}"
            ) from exc

    answer: dict[str, Any] | None = None
    try:
        answer = _parse_sentinel(result.stdout)
    except DesktopRagError:
        # No usable answer. If ssh itself failed, its own error is the honest
        # explanation and beats "no result line".
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise DesktopRagError(
                "UNREACHABLE",
                f"ssh to {cfg['user']}@{cfg['host']} exited "
                f"{result.returncode}: {detail or 'no output'}",
            ) from None
        raise

    if not answer.get("ok"):
        kind = str(answer.get("kind") or "REMOTE_ERROR")
        raise DesktopRagError(kind, str(answer.get("detail") or "no detail given"))
    return answer


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def desktop_available(runner: Runner | None = None) -> bool:
    """Cheap reachability check. Never raises, never hangs a chat turn — one
    ``echo`` over SSH with an 8s cap and a 10s connect timeout, no Python and
    no vault I/O on the far side.

    False also means "not configured": this answers "can I query the desktop
    right now?", and an unset target is a genuine no.
    """
    cfg = desktop_rag_config()
    if cfg is None:
        return False
    run = runner or _run_ssh_command
    # A constant, argument-free command — nothing user-supplied reaches it.
    cmd = _ssh_base(cfg) + ["echo", "DOURMOUSE_OK"]
    try:
        result = run(cmd, timeout=_AVAILABLE_TIMEOUT, stdin_path=None)
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and "DOURMOUSE_OK" in (result.stdout or "")


def query_desktop_rag(
    query: str, limit: int = 5, runner: Runner | None = None
) -> list[dict[str, Any]]:
    """Embed ``query`` with the vault's OWN model on the desktop, FAISS-search
    the real index there, and return the real matching rows.

    Each hit is a dict with:

    - ``id``              — the true ``hybrid_chunks.id`` (resolved through the
                            position->id map, never a raw FAISS position)
    - ``title``           — the row's title
    - ``chunk_text``      — the row's full chunk text
    - ``source_pipeline`` — which ingest pipeline the row came from
    - ``score``           — a genuine cosine similarity in ``[-1, 1]`` against
                            the index's own stored vector for that hit
    - ``distance``        — FAISS's raw squared-L2 output, unaltered
    - ``position``        — the FAISS position, kept so a human can audit the map
    - ``verify_cosine``   — the receipt that this row really is the one whose
                            vector matched (see the module docstring)

    Raises ``DesktopRagError`` for every failure — NOT_CONFIGURED, UNREACHABLE,
    TIMEOUT, MISSING_DEPENDENCY, VAULT_MISSING, MAPPING_MISMATCH, REMOTE_ERROR,
    BAD_RESPONSE. A genuinely empty result list means the vault was really
    searched and really matched nothing; it is never returned in place of an
    error.
    """
    cfg = desktop_rag_config()
    if cfg is None:
        raise DesktopRagError(
            "NOT_CONFIGURED",
            f"{_ENV_HOST}/{_ENV_USER}/{_ENV_KEY} are not all set, so there is "
            "no desktop to query. Nothing was read and nothing was fabricated.",
        )
    if not isinstance(query, str) or not query.strip():
        raise DesktopRagError(
            "BAD_REQUEST", "empty query — refusing to search the vault for nothing."
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise DesktopRagError("BAD_REQUEST", f"limit must be an integer: {limit!r}") from exc
    if limit < 1:
        raise DesktopRagError("BAD_REQUEST", f"limit must be >= 1, got {limit}")

    payload = {
        "mode": "query",
        "query": query,
        "limit": limit,
        "db": cfg["db"],
        "index": cfg["index"],
        "table": cfg["table"],
        "model": cfg["model"],
        "id_filter_sql": cfg["id_filter_sql"],
        "id_order_sql": cfg["id_order_sql"],
        "verify_min_cosine": _VERIFY_MIN_COSINE,
    }
    answer = _remote_call(cfg, payload, cfg["timeout"], runner)
    hits = answer.get("hits")
    if not isinstance(hits, list):
        raise DesktopRagError(
            "BAD_RESPONSE", f"the desktop returned {type(hits).__name__}, not a list of hits."
        )
    return hits


def desktop_rag_status(runner: Runner | None = None) -> dict[str, Any]:
    """``{"ok", "detail", "hint"}`` — the exact shape every entry in
    ``connections.check_connections`` uses, so this can drop straight into
    that report.

    Never raises and never fabricates a green tick: the probe genuinely SSHes
    in and checks, on the real desktop, that ``numpy``/``faiss``/
    ``sentence_transformers`` are importable and that both vault files exist.
    It uses ``importlib.util.find_spec`` rather than importing them, so it
    stays cheap (~1s) — importing ``sentence_transformers`` alone costs 7-32s.
    """
    hint = (
        f"set {_ENV_HOST}, {_ENV_USER} and {_ENV_KEY} (a dedicated SSH key, "
        "never the user's own identity) to reach the desktop's spatial vault"
    )
    cfg = desktop_rag_config()
    if cfg is None:
        return {
            "ok": False,
            "detail": (
                f"NOT CONFIGURED — {_ENV_HOST}/{_ENV_USER}/{_ENV_KEY} not all set"
            ),
            "hint": hint,
        }
    target = f"{cfg['user']}@{cfg['host']}"
    try:
        answer = _remote_call(
            cfg,
            {"mode": "probe", "db": cfg["db"], "index": cfg["index"], "table": cfg["table"]},
            cfg["probe_timeout"],
            runner,
        )
    except DesktopRagError as exc:
        return {
            "ok": False,
            "detail": f"{target} — {exc}",
            "hint": hint,
        }

    deps = answer.get("deps") or {}
    missing = sorted(name for name, present in deps.items() if not present)
    if missing:
        return {
            "ok": False,
            "detail": f"{target} reachable but missing on the desktop: {', '.join(missing)}",
            "hint": (
                "install those on the DESKTOP (where the vault and model live) — "
                "this module never installs anything on the user's machine"
            ),
        }
    rows = answer.get("max_id")
    size_mb = answer.get("index_size_bytes")
    size_note = f" · index {size_mb / (1024 * 1024):.0f} MB" if isinstance(size_mb, (int, float)) else ""
    return {
        "ok": True,
        "detail": (
            f"{target} · vault reachable · {cfg['table']} max id {rows}{size_note} "
            f"· embeds with {cfg['model']}"
        ),
        "hint": hint,
    }


def format_desktop_rag(
    query: str, limit: int = 5, runner: Runner | None = None
) -> str:
    """Plain-text rendering for the tool-call path, mirroring
    ``shared_rag.format_merged_result``'s shape. Never raises: an error
    becomes a visible, honest line (``NOT CONFIGURED`` / the real error text),
    never a fabricated hit and never a silent empty answer."""
    try:
        hits = query_desktop_rag(query, limit=limit, runner=runner)
    except DesktopRagError as exc:
        if exc.kind == "NOT_CONFIGURED":
            return f"NOT CONFIGURED: {exc}"
        return f"DESKTOP VAULT UNAVAILABLE — {exc}"
    if not hits:
        return (
            f"DESKTOP SPATIAL VAULT search for {query!r}: no matches (honest — "
            "the real index was searched and returned nothing)."
        )
    lines = [f"DESKTOP SPATIAL VAULT ({len(hits)} hits) for {query!r}:"]
    for i, hit in enumerate(hits, start=1):
        lines.append(
            f"[{i}] score={hit.get('score', 0.0):.3f} id={hit.get('id')} "
            f"pipeline={hit.get('source_pipeline')} title={hit.get('title')!r}\n"
            f"    {hit.get('chunk_text', '')}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The program that actually runs ON the desktop
# --------------------------------------------------------------------------- #
#
# Sent over stdin to a plain ``python -``. It only ever READS the vault (the
# SQLite connection is opened read-only via a ``mode=ro`` URI — the vault is a
# live-growing file this codebase does not own). The single file it writes is
# the position->id map cache in the desktop's own temp dir, and failing to
# write it is swallowed.
#
# It answers on ONE line prefixed with the sentinel because loading the model
# prints a load report and a progress bar to stdout that would otherwise be
# indistinguishable from the result.

_REMOTE_SCRIPT = r'''
import base64
import json
import os
import sys

SENTINEL = "__DOURMOUSE_DESKTOP_RAG__"


def emit(obj):
    sys.stdout.write("\n" + SENTINEL + json.dumps(obj) + "\n")
    sys.stdout.flush()


def fail(kind, detail):
    emit({"ok": False, "kind": kind, "detail": detail})
    raise SystemExit(0)


try:
    cfg = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
except Exception as exc:
    emit({"ok": False, "kind": "BAD_REQUEST", "detail": "undecodable payload: %r" % (exc,)})
    raise SystemExit(0)

# The model is already in this machine's HuggingFace cache (verified live).
# Pin it offline so a query can never turn into a surprise download on the
# user's machine, and never phone home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DB = cfg["db"]
INDEX = cfg["index"]
TABLE = cfg["table"]
MODE = cfg.get("mode", "query")

import importlib.util

DEPS = ("numpy", "faiss", "sentence_transformers")


def dep_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


if MODE == "probe":
    deps = dict((name, dep_present(name)) for name in DEPS)
    out = {"ok": True, "deps": deps, "python": sys.version.split()[0]}
    out["db_present"] = os.path.isfile(DB)
    out["index_present"] = os.path.isfile(INDEX)
    if not out["db_present"] or not out["index_present"]:
        fail(
            "VAULT_MISSING",
            "on this desktop db_present=%s (%s) index_present=%s (%s)"
            % (out["db_present"], DB, out["index_present"], INDEX),
        )
    out["index_size_bytes"] = os.path.getsize(INDEX)
    out["db_size_bytes"] = os.path.getsize(DB)
    try:
        import sqlite3

        con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
        # MAX over an INTEGER PRIMARY KEY is an index lookup, not a table scan
        # (measured at 0.00s on the real 2.98 GB file) -- COUNT(*) would be a
        # full scan and this probe must stay cheap.
        out["max_id"] = con.execute('SELECT MAX("id") FROM "%s"' % TABLE).fetchone()[0]
        con.close()
    except Exception as exc:
        fail("REMOTE_ERROR", "could not read %s from %s: %r" % (TABLE, DB, exc))
    emit(out)
    raise SystemExit(0)

# ---- query mode -----------------------------------------------------------

for path, what in ((DB, "vault database"), (INDEX, "FAISS index")):
    if not os.path.isfile(path):
        fail("VAULT_MISSING", "%s not found at %s on the desktop" % (what, path))

missing = [name for name in DEPS if not dep_present(name)]
if missing:
    fail(
        "MISSING_DEPENDENCY",
        "not installed on the desktop: %s. Install them THERE (the vault and "
        "the model live there); nothing is installed automatically." % ", ".join(missing),
    )

try:
    import hashlib
    import sqlite3
    import tempfile

    import numpy as np
    import faiss
    from sentence_transformers import SentenceTransformer
except Exception as exc:
    fail("MISSING_DEPENDENCY", "import failed on the desktop: %r" % (exc,))

try:
    index = faiss.read_index(INDEX)
except Exception as exc:
    fail("REMOTE_ERROR", "%s did not load as a FAISS index: %r" % (INDEX, exc))

ntotal = int(index.ntotal)
dim = int(index.d)
if ntotal <= 0:
    fail("REMOTE_ERROR", "%s reports 0 vectors -- nothing to search." % INDEX)

try:
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    max_id = con.execute('SELECT MAX("id") FROM "%s"' % TABLE).fetchone()[0]
except Exception as exc:
    fail("REMOTE_ERROR", "could not open %s read-only: %r" % (DB, exc))

# ---- position -> id map ---------------------------------------------------
# A bare IndexFlatL2 has no id map: search() returns 0-based POSITIONS. The
# map is rebuilt from SQL in the index's real build order. That sort costs
# ~38s cold over a million rows, so it is cached in this machine's own temp
# dir and keyed on (paths, table, filter, order, ntotal, max_id) -- any growth
# of either the index or the table changes the key and rebuilds it.

key_material = json.dumps(
    [DB, TABLE, cfg["id_filter_sql"], cfg["id_order_sql"], ntotal, max_id],
    sort_keys=True,
)
cache_name = "dourmouse_desktop_rag_idmap_%s.i64" % hashlib.sha256(
    key_material.encode("utf-8")
).hexdigest()[:32]
cache_path = os.path.join(tempfile.gettempdir(), cache_name)

position_to_id = None
try:
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) == ntotal * 8:
        position_to_id = np.fromfile(cache_path, dtype="<i8")
        if position_to_id.shape[0] != ntotal:
            position_to_id = None
except Exception:
    position_to_id = None  # a bad cache is never fatal; just rebuild

if position_to_id is None:
    where = " WHERE " + cfg["id_filter_sql"] if cfg["id_filter_sql"] else ""
    sql = 'SELECT "id" FROM "%s"%s ORDER BY %s LIMIT ?' % (
        TABLE, where, cfg["id_order_sql"],
    )
    try:
        rows = con.execute(sql, (ntotal,)).fetchall()
    except Exception as exc:
        fail(
            "REMOTE_ERROR",
            "the position->id map query failed (%r). SQL: %s" % (exc, sql),
        )
    if len(rows) < ntotal:
        fail(
            "MAPPING_MISMATCH",
            "the id filter selects %d rows but the index holds %d vectors, so "
            "positions cannot be resolved. The vault has grown or changed shape "
            "-- update DOURMOUSE_DESKTOP_RAG_ID_FILTER_SQL / _ID_ORDER_SQL."
            % (len(rows), ntotal),
        )
    position_to_id = np.asarray([int(r[0]) for r in rows], dtype="<i8")
    try:
        position_to_id.tofile(cache_path)
    except Exception:
        pass  # a read-only temp dir just means the slow path every time

# ---- embed + search -------------------------------------------------------

try:
    model = SentenceTransformer(cfg["model"])
except Exception as exc:
    fail(
        "REMOTE_ERROR",
        "could not load %s on the desktop (offline mode is on, so it must "
        "already be cached there): %r" % (cfg["model"], exc),
    )

try:
    qvec = np.asarray(model.encode([cfg["query"]]), dtype="float32")
except Exception as exc:
    fail("REMOTE_ERROR", "embedding the query failed: %r" % (exc,))

if qvec.shape[1] != dim:
    fail(
        "MAPPING_MISMATCH",
        "%s embeds at %d dims but the index is %d-dimensional -- these are "
        "different vector spaces and comparing them would be meaningless."
        % (cfg["model"], qvec.shape[1], dim),
    )

qnorm = float(np.linalg.norm(qvec[0]))
if qnorm <= 0.0:
    fail("REMOTE_ERROR", "the query embedded to a zero vector; nothing to compare.")
q_unit = qvec[0] / qnorm

try:
    distances, positions = index.search(qvec, int(cfg["limit"]))
except Exception as exc:
    fail("REMOTE_ERROR", "FAISS search failed: %r" % (exc,))

# ---- resolve rows, and PROVE each one ------------------------------------
# For every hit, pull the index's own stored vector back out and re-embed the
# SQL row the map claims it belongs to. A stale map returns real-looking but
# WRONG rows -- this is the check that catches that instead of shipping them.

candidates = []
for raw_distance, position in zip(distances[0].tolist(), positions[0].tolist()):
    if position < 0:
        continue  # FAISS's own "no result" sentinel
    if position >= position_to_id.shape[0]:
        continue  # no known id for this position
    row_id = int(position_to_id[position])
    row = con.execute(
        'SELECT "id", "title", "chunk_text", "source_pipeline" FROM "%s" WHERE "id" = ?'
        % TABLE,
        (row_id,),
    ).fetchone()
    if row is None:
        continue  # a live-growing vault can go stale between writes
    candidates.append((float(raw_distance), int(position), row))
con.close()

hits = []
if candidates:
    try:
        stored = np.asarray(
            [index.reconstruct(int(position)) for _, position, _ in candidates],
            dtype="float32",
        )
        fresh = np.asarray(
            model.encode([row[2] for _, _, row in candidates]), dtype="float32"
        )
    except Exception as exc:
        fail("REMOTE_ERROR", "could not verify the position->id map: %r" % (exc,))

    threshold = float(cfg.get("verify_min_cosine", 0.98))
    for i, (raw_distance, position, row) in enumerate(candidates):
        sv = stored[i]
        fv = fresh[i]
        sn = float(np.linalg.norm(sv))
        fn = float(np.linalg.norm(fv))
        if sn <= 0.0 or fn <= 0.0:
            fail("REMOTE_ERROR", "a zero vector at position %d; cannot verify." % position)
        verify = float(np.dot(sv, fv) / (sn * fn))
        if verify < threshold:
            fail(
                "MAPPING_MISMATCH",
                "position %d was mapped to id %d, but that row re-embeds to "
                "cosine %.4f against the vector actually stored there (needs "
                ">= %.2f). The position->id map is stale, so these rows would "
                "be the WRONG rows -- refusing to return them. Update "
                "DOURMOUSE_DESKTOP_RAG_ID_FILTER_SQL / _ID_ORDER_SQL to match "
                "how the index is built now."
                % (position, int(row[0]), verify, threshold),
            )
        hits.append(
            {
                "id": int(row[0]),
                "title": row[1],
                "chunk_text": row[2],
                "source_pipeline": row[3],
                # A true cosine against the index's OWN stored vector -- not a
                # rank normalized within this result set.
                "score": float(np.dot(q_unit, sv / sn)),
                "distance": raw_distance,
                "position": position,
                "verify_cosine": verify,
            }
        )

hits.sort(key=lambda h: h["score"], reverse=True)
emit(
    {
        "ok": True,
        "hits": hits,
        "meta": {
            "ntotal": ntotal,
            "dim": dim,
            "max_id": max_id,
            "model": cfg["model"],
            "idmap_cached": os.path.isfile(cache_path),
        },
    }
)
'''
