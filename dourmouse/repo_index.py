"""Repo knowledge index (v4.1, P6) — ingest ANY codebase into memory.

Gives the roster durable, deterministic knowledge of the real ATLAS repo by
default, and of ANY other project on request: ``scan_repo`` walks a root and
writes a curated digest of every meaningful file into the long-term memory
store under a scoped source key — ``"repo"`` for the ATLAS default, or
``"repo:<folder-slug>"`` derived from the folder name when a tool passes an
explicit ``path`` — so multiple projects stay scoped and never mix. Scoping
is EXACT (``MemoryStore.search`` filters the joined row by source equality,
never by an FTS5 column filter), so ``repo`` can never match ``repo:proj``. Later
questions like "why did we change the risk parameters" surface the actual
CHANGELOG/docs/reports that recorded the change. Deterministic (Rule 2.8):
pure filesystem reads + FTS5 recall, never an LLM judgment.

Honesty (Rule 2.2): no ``ATLAS_REPO_PATH`` -> NOT CONFIGURED; files that are
binary, enormous, or inside excluded dirs are skipped — never guessed.
Idempotent: a re-scan of an unchanged repo adds/updates nothing (facts carry
a ``META mtime=.. size=..`` line, and MemoryStore upserts by (source, title)).

Exclusions (mirrors .gitignore): .git, .venv, venv, .pytest_cache,
.mypy_cache, .ruff_cache, node_modules, __pycache__, .freebuff, workspace,
.idea, .vscode, and the ATLAS ``data`` tree (raw archives/logs).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading as _threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dourmouse import path_identity
from dourmouse.memory_store import MemoryStore

_log = logging.getLogger(__name__)

DEFAULT_SOURCE = "repo"


def _slug(name: str) -> str:
    """A safe scope fragment for a folder name ('My Proj!' -> 'my-proj').

    Underscores are PRESERVED on purpose: distinct folder names like
    ``my_proj`` and ``my-proj`` must never collapse into one scope (the scan
    prune deletes by exact source, so a collision would let one project's
    rescan delete another's facts)."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "-", name).strip("-").lower()
    return slug or "project"


def source_for_root(root: Path) -> str:
    """Scoped source key for a project root: ``repo:<folder-slug>``, derived
    from the folder name so distinct projects never share a scope.

    Safe with the colon because scoping is exact: ``MemoryStore.search``
    filters by source EQUALITY on the joined row (``f.source = ?``), never
    by an FTS5 column filter, so ``repo`` can never match ``repo:proj``."""
    return f"{DEFAULT_SOURCE}:{_slug(root.resolve().name)}"

_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "node_modules", "__pycache__", ".freebuff", "workspace", ".idea",
        ".vscode", "data",  # ATLAS data tree: raw archives, backfill logs
    }
)

_SKIP_FILES = frozenset(
    {".env", ".env.example", "desktop-v2.db", "desktop-v2.db-shm", "desktop-v2.db-wal"}
)

# Credential and secret material: never indexed (the digests are recalled into
# later model prompts). Directory names are skipped during the walk; file names
# are matched case-insensitively.
_CREDENTIAL_DIRS = frozenset({".aws", ".ssh", ".gnupg", ".kube", ".docker", ".azure", ".gcloud"})
# Credential files are recognised by WHOLE name words, not substrings (W2R-9): ``credentials.json``,
# ``client_secret_1.json`` and ``api_keys.txt`` are credential files; ``tokenizer.py``, ``token_bucket.ts``,
# ``design-tokens.json``, ``password_reset.html`` and ``SECRET_SCAN.md`` are ordinary source and docs.
# The file stem is lower-cased, separators and digits are dropped, and what is left must be a credential word
# optionally wrapped in the usual qualifiers (client, google, prod, ...).
_CREDENTIAL_NAME_RE = re.compile(
    r"(^auth\.json$|^\.env(\..+)?$|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|keystore|jks)$"
    r"|^\.(netrc|npmrc|pgpass|pypirc|git-credentials)$)",
    re.IGNORECASE,
)
_CRED_WORD = (
    r"(?:credentials?|secrets?|tokens?|passwords?|passwd|apikeys?|serviceaccount(?:keys?)?|privatekeys?|"
    r"accesskeys?|secretkeys?)"
)
_CRED_STEM_RE = re.compile(
    r"^(?:client|service|my|app|prod|production|dev|staging|local|user|db|api|github|google|aws|gcp|"
    r"oauth|access|refresh|auth|bearer|session|private|ssh)*" + _CRED_WORD +
    r"(?:local|prod|production|dev|staging|backup|old|new|example|sample|template)*$"
)
# Documents are matched only on the bare word (secrets.md, passwords.md): an ordinary page about tokens or
# secrets (auth_tokens.md, SECRET_SCAN.md) is documentation. Stylesheets carry design tokens, never secrets.
_DOC_SUFFIXES = frozenset({".md", ".rst", ".adoc", ".html", ".htm"})
_STYLE_SUFFIXES = frozenset({".css", ".scss", ".less", ".styl", ".svg"})
_CRED_BARE_STEM_RE = re.compile(r"^" + _CRED_WORD + r"$")


def _is_credential_name(fn: str) -> bool:
    """True for a file name that marks credential or secret material (see ``_CREDENTIAL_NAME_RE``)."""
    if _CREDENTIAL_NAME_RE.search(fn):
        return True
    path = Path(fn)
    suffix = path.suffix.lower()
    if suffix in _STYLE_SUFFIXES:
        return False
    stem = re.sub(r"[^a-z0-9]", "", path.stem.lower().lstrip(".")).strip("0123456789")
    if suffix in _DOC_SUFFIXES:
        return bool(_CRED_BARE_STEM_RE.match(stem))
    # A name with no real extension (``github_token``, ``passwd``) is its whole name.
    if not suffix or path.stem == fn:
        stem = re.sub(r"[^a-z0-9]", "", fn.lower().lstrip(".")).strip("0123456789")
    return bool(_CRED_STEM_RE.match(stem))


_MAX_MD = 2500
_MAX_SKELETON = 1200
_MAX_REPORT = 1500
_MAX_FILE_SIZE = 512_000


# --------------------------------------------------------------------------- #
# Store access (same convention as learn.py / general_roster)
# --------------------------------------------------------------------------- #

def open_repo_store() -> MemoryStore | Exception:
    """The shared long-term store for repo facts, or the honest reason why not.

    Reuses ``learn.open_default_store`` (same gate + path convention) so the
    repo index writes into the SAME database the learn loop and memory agent
    use; its None (disabled/unavailable) maps to an honest NOT CONFIGURED.
    """
    from dourmouse.learn import open_default_store

    store = open_default_store()
    if store is None:
        return RuntimeError(
            "the memory store is unavailable (DOURMOUSE_LEARN off or SQLite "
            "FTS5 missing) — the repo index is NOT CONFIGURED."
        )
    return store


# --------------------------------------------------------------------------- #
# Walking + digestion (deterministic, Rule 2.8)
# --------------------------------------------------------------------------- #

def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def _collect_files(root: Path) -> list[Path]:
    """Every ingestible file under root, honoring the exclusion set."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _EXCLUDED_DIRS and d.lower() not in _CREDENTIAL_DIRS
        )
        for fn in sorted(filenames):
            if fn in _SKIP_FILES or _is_credential_name(fn) or fn.endswith(
                (".pyc", ".zip", ".gz", ".db", ".bi5", ".repo-meta.json")
            ):
                continue
            p = Path(dirpath) / fn
            if p.is_symlink():
                # A link inside an allowed root can point anywhere (a key file, another project): never read
                # through it, as the device-wiki walker does not.
                continue
            try:
                if p.stat().st_size > _MAX_FILE_SIZE or _is_binary(p):
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _python_skeleton(text: str, cap: int) -> str:
    """The useful surface of a .py: module docstring + top-level signatures."""
    doc = re.search(r'"""(.*?)"""', text, re.DOTALL)
    head = doc.group(1).strip()[:600] if doc else ""
    sigs = [
        ln.strip()
        for ln in text.splitlines()
        if re.match(r"^(async\s+)?(def|class)\s+\w", ln.strip())
    ]
    parts: list[str] = []
    if head:
        parts.append(f"DOCSTRING:\n{head}")
    if sigs:
        parts.append("SIGNATURES:\n" + "\n".join(sigs[:80]))
    body = "\n\n".join(parts).strip()
    return body[:cap] + ("…" if len(body) > cap else "")


def _digest_many(path: Path) -> list[tuple[str, str, str]]:
    """(title_suffix, body, kind) digests for one file — MANY for changelogs.

    A changelog is split into per-section facts (``CHANGELOG.md: <section>``)
    so a decision recorded years ago in the middle of the file is a first-class
    retrievable fact instead of being buried past a head cap. Every other file
    yields exactly one digest with an empty title suffix. Empty result means
    'skip honestly' (binary, unsupported type, unreadable)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            return [("", _python_skeleton(path.read_text(encoding="utf-8", errors="replace"), _MAX_SKELETON), "python")]
        if suffix in (".md", ".markdown"):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                return []
            name = path.name.upper()
            if name.startswith(("CHANGELOG", "CHANGES")):
                sections = _section_digest(text)
                # A changelog with NO '## ' sections must not vanish: fall
                # back to one flat digest so the file stays indexed.
                if sections:
                    return sections
                return [("", text[:_MAX_MD] + ("…" if len(text) > _MAX_MD else ""), "markdown")]
            return [("", text[:_MAX_MD] + ("…" if len(text) > _MAX_MD else ""), "markdown")]
        if suffix in (".json", ".html", ".csv", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg"):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                return []
            return [("", text[:_MAX_REPORT] + ("…" if len(text) > _MAX_REPORT else ""), "text")]
    except OSError:
        return []
    return []


_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _section_digest(text: str) -> list[tuple[str, str, str]]:
    """Split a changelog into per-section digests: title suffix ": <heading>"
    (the first ``## `` line of each section), body = heading + section text
    capped at ``_MAX_MD``. The pre-first-section intro (usually just the doc
    title) is dropped — it carries no retrievable decision."""
    out: list[tuple[str, str, str]] = []
    seen_titles: dict[str, int] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        title = f": {heading[:120]}"
        # Repeated headings (several "## Fixed" blocks) must not share a
        # (source, title) key or the later ones overwrite / are skipped as
        # unchanged; the nth repeat gets " #n".
        seen_titles[title] = seen_titles.get(title, 0) + 1
        if seen_titles[title] > 1:
            title = f"{title} #{seen_titles[title]}"
        full = f"{heading}\n{body}"
        out.append((title, full[:_MAX_MD] + ("…" if len(full) > _MAX_MD else ""), "markdown"))
    return out


def _meta_line(path: Path) -> str:
    st = path.stat()
    return f"META mtime={int(st.st_mtime_ns)} size={st.st_size}"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def scan_repo(
    store: MemoryStore, root: Path | None = None, source: str = DEFAULT_SOURCE
) -> dict[str, Any]:
    """Ingest a curated digest of a repo into the memory store.

    Idempotent: unchanged files are skipped (no writes, no updated_at bump);
    changed files are re-ingested in place; new files are added. ``root``
    defaults to ``ATLAS_REPO_PATH`` (raises AtlasNotConfiguredError honestly
    when unset/invalid). ``source`` is the scoped key facts land under (and
    the ONLY key this scan's prune touches) — pass a derived
    ``repo:<folder>`` for a non-default project. Returns ``{source, scanned,
    added, updated, skipped, unchanged, removed, total_facts}``.
    """
    from dourmouse.atlas.atlas_ops import get_atlas_repo_path

    if root is None:
        root = get_atlas_repo_path()
    from dourmouse.governance import DlpFilter

    dlp = DlpFilter()
    stats: dict[str, Any] = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "unchanged": 0}
    walked: set[str] = set()
    produced_by_rel: dict[str, set[str]] = {}
    for p in _collect_files(root):
        # POSIX-normalized relative path: repo facts are shared knowledge that
        # must look identical on macOS and Windows (a Windows backslash path
        # would break cross-platform lookups and the PATH= body line).
        rel = p.relative_to(root).as_posix()
        walked.add(rel)
        digests = _digest_many(p)
        if not digests:
            stats["skipped"] += 1
            produced_by_rel[rel] = set()  # walked but produced nothing
            continue
        stats["scanned"] += 1
        meta = _meta_line(p)
        produced: set[str] = set()
        for title_suffix, body, kind in digests:
            title = rel + title_suffix
            produced.add(title)
            existing = store.get(source, title)
            if existing is not None and existing["body"].startswith(meta):
                stats["unchanged"] += 1
                continue
            # Digests are recalled into later prompts that go to cloud models:
            # credential-shaped content is redacted before it is stored.
            body, _hits = dlp.redact(body)
            new_body = f"{meta}\nKIND={kind}\nPATH={rel}\n\n{body}"
            store.remember(source, title, new_body)
            stats["updated" if existing is not None else "added"] += 1
        produced_by_rel[rel] = produced
    # Prune — two distinct cases, so a flaky scan can never destroy knowledge:
    #  (a) the file was NOT walked at all this scan -> definitively gone
    #      (deleted, or excluded by size/binary) -> drop its facts;
    #  (b) the file WAS walked and produced facts, but this title was not
    #      among them -> digest shape changed (e.g. flat fact became sections)
    #      -> drop the obsolete fact.
    # A file that was walked but produced NOTHING (transient read error, or
    # a skip) keeps its existing facts — Rule 2.2: never lose knowledge to a
    # transient failure.
    removed = 0
    # Pruning needs a store whose all_facts() and delete() read and write the
    # SAME backing data as remember(). A remote-backed fallback store reads
    # its LOCAL copy and cannot delete, so pruning it would act on the wrong
    # facts (or raise); skip it and say so.
    prunable = isinstance(store, MemoryStore)
    if not prunable:
        stats["prune_skipped"] = (
            "this store's reads and writes go to different backends; removed files were not pruned"
        )
    for fact in store.all_facts() if prunable else []:
        if fact["source"] != source:
            continue  # prune only this project's scope, never another's
        rel = _fact_rel(fact["body"])
        if rel not in walked:
            # definitively gone (deleted or excluded by size/binary)
            if store.delete(source, fact["title"]):
                removed += 1
            continue
        produced = produced_by_rel.get(rel, set())
        if produced and fact["title"] not in produced and store.delete(source, fact["title"]):
            removed += 1
    stats["removed"] = removed
    stats["source"] = source
    stats["total_facts"] = store.count(source=source)
    return stats


def _fact_rel(body: str) -> str:
    """The file a repo fact belongs to, from its ``PATH=`` body line (the
    exact title scheme is irrelevant — this is stable across schema changes)."""
    for line in (body or "").splitlines():
        if line.startswith("PATH="):
            return line[len("PATH="):].strip()
    return ""


def _fact_kind(body: str) -> str:
    """The digest kind of a repo fact (python/markdown/text), from ``KIND=``."""
    for line in (body or "").splitlines():
        if line.startswith("KIND="):
            return line[len("KIND="):].strip()
    return ""


# --------------------------------------------------------------------------- #
# Scan meta (sidecar JSON next to the store DB — never pollutes fact counts)
# --------------------------------------------------------------------------- #

def _meta_path(store: MemoryStore, source: str = DEFAULT_SOURCE) -> Path:
    """Per-scope sidecar: the ATLAS default keeps the legacy name, other
    projects get ``<db>.repo-meta-<slug>.json`` so scopes never collide."""
    if source == DEFAULT_SOURCE:
        return Path(str(store.db_path) + ".repo-meta.json")
    return Path(str(store.db_path) + f".repo-meta-{_slug(source)}.json")


_SCAN_META_LOCK = _threading.Lock()


def save_scan_meta(
    store: MemoryStore, stats: dict[str, Any], root: Path, source: str = DEFAULT_SOURCE
) -> None:
    """Persist the last-scan summary as a sidecar JSON next to the store DB.

    One sidecar per scope (see ``_meta_path``) so projects never overwrite
    each other's history. Kept OUT of the fact store so a scan meta can never
    pollute the fact count or the idempotency/prune logic. Atomic (unique tmp
    + rename) so a crash mid-write leaves the previous meta intact, and
    concurrent saves under the threaded server never corrupt each other's
    tmp file."""
    payload = {
        "when": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "scanned": int(stats.get("scanned", 0)),
        "added": int(stats.get("added", 0)),
        "updated": int(stats.get("updated", 0)),
        "skipped": int(stats.get("skipped", 0)),
        "unchanged": int(stats.get("unchanged", 0)),
        "removed": int(stats.get("removed", 0)),
        "total_facts": int(stats.get("total_facts", 0)),
    }
    path = _meta_path(store, source)
    tmp = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{_threading.get_ident()}"
    )
    with _SCAN_META_LOCK:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def load_scan_meta(
    store: MemoryStore, source: str = DEFAULT_SOURCE
) -> dict[str, Any] | None:
    """The last-scan summary for one scope, or None honestly (never
    scanned / unreadable)."""
    try:
        data = json.loads(_meta_path(store, source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def repo_facts(
    store: MemoryStore, limit: int = 12, source: str = DEFAULT_SOURCE
) -> list[dict[str, Any]]:
    """The newest indexed facts for one project scope, with file path + kind,
    for the HUD panel. Newest-first (all_facts is id-ascending; the latest
    ingest is last). Titles/paths are displayed as-is — no body text, so the
    panel stays light."""
    out = [
        {"title": fact["title"], "path": _fact_rel(fact["body"]), "kind": _fact_kind(fact["body"])}
        for fact in store.all_facts()
        if fact["source"] == source
    ]
    out.reverse()
    return out[: max(1, min(int(limit), 50))]


def repo_search(
    store: MemoryStore,
    query: str,
    limit: int = 8,
    source: str = DEFAULT_SOURCE,
) -> list[dict[str, Any]]:
    """FTS5 recall scoped to ONE project's repo facts (its ``source`` key).

    The query is distilled to its distinctive terms first (same deterministic
    pass as the learn loop / semantic fallback) so conversational phrasing
    ("why did we change the risk parameters") recalls instead of being
    AND-matched word-by-word.
    """
    from dourmouse.learn import distill_query

    distilled = distill_query(query)
    if not distilled:
        return []
    return store.search(distilled, limit=limit, source=source)


def repo_status(
    store: MemoryStore, source: str = DEFAULT_SOURCE
) -> dict[str, Any]:
    """How many facts are indexed for one project's scope (and its key)."""
    return {"source": source, "facts": store.count(source=source)}


# --------------------------------------------------------------------------- #
# Tool handlers (same shape as every roster tool: dict in, str out)
# --------------------------------------------------------------------------- #

def _allowed_scan_roots() -> list[Path]:
    """Folders a model-chosen ``path`` may live in: Dourmouse's own folder,
    the workspace, the ATLAS repo, the projects on the owner's bookshelf, and
    any extra folders the owner lists in ``DOURMOUSE_REPO_SCAN_ROOTS``
    (``os.pathsep``-separated)."""
    roots: list[Path] = [Path(__file__).resolve().parent.parent]
    try:
        from dourmouse.config import workspace_dir

        roots.append(Path(workspace_dir()))
    except Exception as exc:  # noqa: BLE001 -- no workspace means no extra root
        _log.debug("workspace not added to the repo scan roots: %s", exc)
    try:
        from dourmouse.atlas.atlas_ops import get_atlas_repo_path

        roots.append(Path(get_atlas_repo_path()))
    except Exception as exc:  # noqa: BLE001 -- ATLAS not configured: not a root
        _log.debug("ATLAS repo not added to the repo scan roots: %s", exc)
    try:
        from dourmouse.project_bookkeeper import _load_store, _public_view, _store_path

        for rec in _public_view(_load_store(_store_path())).get("projects") or []:
            if rec.get("path"):
                roots.append(Path(str(rec["path"])).expanduser())
    except Exception as exc:  # noqa: BLE001 -- an unreadable bookshelf means no extra roots
        _log.debug("bookshelf not read for the repo scan roots: %s", exc)
    for extra in os.environ.get("DOURMOUSE_REPO_SCAN_ROOTS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(extra.strip()).expanduser())
    return roots


def _resolve_scan_arg(
    arguments: dict[str, Any], *, confine: bool = False
) -> tuple[Path, str] | str | None:
    """Resolve a tool call's optional ``path`` to (root, derived source); an
    honest error string when it isn't a real directory; or None when no path
    was given (the caller falls back to the env default)."""
    raw = str(arguments.get("path") or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        return f"ERROR: path {raw!r} is not a directory."
    if confine and not any(path_identity.is_within(root, r) for r in _allowed_scan_roots()):
        return (
            f"ERROR: path {raw!r} is outside the folders atlas_repo_scan may index "
            "(Dourmouse's folder, the workspace, the ATLAS repo, projects on the "
            "bookshelf, or a folder listed in DOURMOUSE_REPO_SCAN_ROOTS). Nothing was indexed."
        )
    return root, source_for_root(root)


def _repo_scan_tool(arguments: dict[str, Any]) -> str:
    store = open_repo_store()
    if isinstance(store, Exception):
        return f"REPO INDEX (honest): NOT CONFIGURED — {store}"
    try:
        target = _resolve_scan_arg(arguments, confine=True)
        if isinstance(target, str):
            return f"REPO INDEX (honest): {target}"
        if target is None:
            from dourmouse.atlas.atlas_ops import AtlasNotConfiguredError, get_atlas_repo_path

            try:
                root = get_atlas_repo_path()
            except AtlasNotConfiguredError as exc:
                return f"REPO INDEX (honest): NOT CONFIGURED — {exc}"
            source = DEFAULT_SOURCE
        else:
            root, source = target
        stats = scan_repo(store, root, source=source)
    finally:
        store.close()
    return (
        f"REPO INDEXED (source={source}):\n"
        f"  root: {root}\n"
        f"  scanned: {stats['scanned']}  added: {stats['added']}  "
        f"updated: {stats['updated']}  unchanged: {stats['unchanged']}  "
        f"skipped: {stats['skipped']}  removed: {stats['removed']}\n"
        f"  total facts in memory: {stats['total_facts']}\n"
        f"  (search/status for this project: pass source={source!r})"
    )


def _repo_search_tool(arguments: dict[str, Any]) -> str:
    store = open_repo_store()
    if isinstance(store, Exception):
        return f"REPO SEARCH (honest): NOT CONFIGURED — {store}"
    try:
        query = str(arguments.get("query", "") or "").strip()
        if not query:
            return "ERROR: repo_search requires a non-empty 'query'."
        source = DEFAULT_SOURCE
        src_arg = str(arguments.get("source") or "").strip()
        if src_arg:
            source = src_arg
        else:
            target = _resolve_scan_arg(arguments)
            if isinstance(target, str):
                return f"REPO SEARCH (honest): {target}"
            if target is not None:
                source = target[1]
        limit = int(arguments.get("max_results", 8))
        hits = repo_search(store, query, limit, source=source)
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."
    finally:
        store.close()
    if not hits:
        return (
            f"REPO SEARCH: no matches in source={source!r} repo facts (honest)."
        )
    lines = [f"REPO SEARCH RESULTS ({len(hits)}) in source={source!r}:"]
    for h in hits:
        lines.append(f"- [{h['source']}] {h['title']} (score {h['score']})\n    {h['snippet']}")
    return "\n".join(lines)


def _repo_status_tool(arguments: dict[str, Any]) -> str:
    store = open_repo_store()
    if isinstance(store, Exception):
        return f"REPO INDEX STATUS (honest): NOT CONFIGURED — {store}"
    try:
        source = str(arguments.get("source") or "").strip() or DEFAULT_SOURCE
        status = repo_status(store, source=source)
    finally:
        store.close()
    return f"REPO INDEX STATUS: {status['facts']} facts indexed (source='{status['source']}')."


def build_repo_tool_specs() -> list[Any]:
    """ToolSpecs for the v4.1 repo-index tools (lazy import, no cycles)."""
    from dourmouse.dispatch import ToolSpec

    def _spec(
        name: str, description: str, handler: Callable[[dict[str, Any]], str], props: dict[str, Any]
    ) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": props,
                "required": [],
            },
            handler=handler,
        )

    return [
        _spec(
            "atlas_repo_scan",
            "Index a repo (README, CHANGELOG, docs, reports, python skeletons) "
            "into long-term memory under a scoped source key. Optional 'path' "
            "points at ANY project (facts go under source 'repo:<folder name>'); "
            "without it, indexes the ATLAS repo (ATLAS_REPO_PATH) as 'repo'. "
            "Idempotent — re-running only picks up changes.",
            _repo_scan_tool,
            {
                "path": {
                    "type": "string",
                    "description": "Optional repo root to index. Defaults to "
                    "ATLAS_REPO_PATH. Stored under a derived scoped source "
                    "'repo:<folder name>'",
                }
            },
        ),
        _spec(
            "atlas_repo_search",
            "Search one project's indexed repo facts — why a decision was "
            "made, what a module does, which report said what. Pass 'source' "
            "(or 'path' to derive it) to scope to a non-default project; "
            "defaults to the ATLAS repo facts (source='repo').",
            _repo_search_tool,
            {
                "query": {"type": "string"},
                "source": {"type": "string", "description": "Scoped source key, e.g. 'repo:myproject' (see atlas_repo_scan output)"},
                "path": {"type": "string", "description": "Alternative to 'source': a repo root whose folder name derives the source"},
                "max_results": {"type": "integer", "default": 8},
            },
        ),
        _spec(
            "atlas_repo_status",
            "How many facts are indexed for one project's scope (default: "
            "the ATLAS repo, source='repo').",
            _repo_status_tool,
            {
                "source": {
                    "type": "string",
                    "description": "Optional scoped source key, e.g. 'repo:myproject'",
                }
            },
        ),
    ]
