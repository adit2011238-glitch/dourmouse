"""self_extensions.py -- Domain D: Dourmouse drafts new tools for itself,
a human approves or rejects, never the model (2026-09-18).

docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md Domain D calls this "the
single most architecturally sensitive item" in the whole spec -- a system
that writes and registers its own new tools needs a real review gate, or
it becomes an unbounded-trust problem. The design here follows that
document's own four constraints literally:

  1. A draft is written to a real file, registered nowhere automatically.
  2. It requires an explicit human approval step before it can ever become
     a live, callable tool -- there is NO "approve" tool in this module's
     own chat-facing surface (agent_smith in general_roster.py), on
     purpose. Approval is reachable only through webui.py's
     POST /api/self_extensions/approve, itself only reachable from a
     human clicking APPROVE in the UI. The model can draft; only a human
     approves.
  3. An approved tool is ALWAYS forced to Permission.REQUIRES_CONFIRMATION
     regardless of what the draft claims for itself -- it can never grant
     itself REGULAR (unattended) execution.
  4. Approval runs the draft's own real test file through a real pytest
     subprocess before the tool is considered live; a failing test blocks
     approval rather than silently merging broken code. Every real
     approval is appended to <workspace>/self_extensions/CHANGELOG.md, a
     permanent, human-readable, per-installation changelog.

Storage is workspace-relative (``<workspace>/self_extensions/``), the same
per-installation convention ``schedules.py``/``goals.py`` already use --
NOT inside the git-tracked ``dourmouse/`` package. A self-added tool is a
property of one installation, not something this change silently ships to
every other Dourmouse install, and it must never tempt an auto-commit of
AI-generated code into the real repository.

An approved tool becomes genuinely callable only after the server process
restarts -- general_roster.py's registry is built once at process start,
exactly like any other Python import; there is no live code-injection into
a running process here, on purpose. This is the same discipline a human
merging a pull request and redeploying already goes through -- Rule 2.8
of this codebase (never fabricate success): claiming a self-added tool is
live before a restart would be exactly that.

What this module deliberately does NOT do: deep static/security analysis
of the drafted Python source. ``_check_syntax`` below is a SYNTAX check
only (``ast.parse``), never a safety guarantee -- claiming otherwise would
itself be a fabricated-confidence bug of the kind this codebase's Rule 2.2
exists to prevent. The real safety boundary is human review of the actual
source before approval, the permanent REQUIRES_CONFIRMATION tier, and a
real passing test -- not an automated content scanner.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

_DRAFTS_FILE = "self_extensions/drafts.jsonl"
_APPROVED_DIR = "self_extensions/approved"
#: Workspace-relative, like every other piece of self-extension state --
#: NOT docs/SELF_EXTENSIONS.md in the git-tracked repo. That was this
#: module's first real design, and it was wrong: a real bug (caught live,
#: not in a unit test) showed webui.py's approval route had no way to
#: pass a repo_root override, so every real approval -- including from
#: automated tests -- silently wrote into the actual tracked repository
#: as a side effect of a button click. Same fix as the rest of this
#: module's own docstring already argues for: a self-added tool's history
#: is a property of one installation, not shared project documentation.
_CHANGELOG_FILE = "self_extensions/CHANGELOG.md"

#: A real Python identifier, and deliberately excludes leading underscores
#: and dunder-looking names -- a self-added tool name should read like any
#: other tool name in the roster (web_search, schedule_recurring, ...),
#: never something that could shadow or look like an internal convention.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _drafts_path() -> Path:
    return workspace_dir() / _DRAFTS_FILE


#: A dedicated override, separate from DOURMOUSE_WORKSPACE on purpose: the
#: drafted test file runs inside its OWN real pytest subprocess, which
#: re-discovers dourmouse/tests/conftest.py and its autouse
#: _workspace_isolated fixture -- that fixture unconditionally overwrites
#: DOURMOUSE_WORKSPACE with a fresh per-test tmp dir (by design, so no
#: ordinary test can ever touch a real workspace), which would otherwise
#: silently point the subprocess's own load_approved() at the wrong
#: directory even though it inherited the parent process's environment.
#: A second, untouched env var sidesteps that collision entirely rather
#: than weakening a safety fixture every other test in this suite relies
#: on. Real bug, caught by actually running the subprocess, not assumed.
_APPROVED_DIR_OVERRIDE_ENV = "DOURMOUSE_SELF_EXT_APPROVED_DIR"


def _approved_dir() -> Path:
    override = os.environ.get(_APPROVED_DIR_OVERRIDE_ENV, "").strip()
    if override:
        return Path(override)
    return workspace_dir() / _APPROVED_DIR


def check_syntax(source: str) -> str | None:
    """Returns an error string if ``source`` is not valid Python, else
    None. A SYNTAX check only -- see the module docstring for why this is
    deliberately not, and does not claim to be, a security review."""
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} (line {exc.lineno})"
    return None


def validate_tool_name(name: str, *, existing_names: frozenset[str]) -> str | None:
    """Returns an error string if ``name`` can't be used for a new tool,
    else None. ``existing_names`` is the live registry's own tool-name set
    (DispatchRegistry already enforces global uniqueness at registration
    time -- checked again here so a draft is rejected with an honest
    reason immediately, not silently accepted and only fail later at
    approval)."""
    if not _NAME_RE.match(name or ""):
        return (
            f"{name!r} is not a usable tool name -- lowercase letters, digits, "
            "underscores only, 3-64 chars, must start with a letter."
        )
    if name in existing_names:
        return f"a tool named {name!r} already exists in the live registry."
    return None


class SelfExtensions:
    """JSONL-backed store of self-extension drafts (workspace/
    self_extensions/drafts.jsonl), mirroring ``schedules.Schedules``'s own
    established shape."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else _drafts_path()

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a corrupt line never breaks the store (Rule 2.2)
        return out

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in entries]
        self._path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def add_draft(
        self,
        *,
        capability_gap: str,
        tool_name: str,
        description: str,
        parameters_schema: dict[str, Any],
        handler_source: str,
        test_source: str,
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        entries = self._load()
        entry_id = f"ext-{len(entries) + 1:03d}"
        entry = {
            "id": entry_id,
            "status": "DRAFTED",
            "capability_gap": capability_gap,
            "tool_name": tool_name,
            "description": description,
            "parameters_schema": parameters_schema,
            "handler_source": handler_source,
            "test_source": test_source,
            "goal_id": goal_id,
            "created_at": _now(),
            "decided_at": None,
            "decision_reason": None,
        }
        entries.append(entry)
        self._save(entries)
        return entry

    def get(self, entry_id: str) -> dict[str, Any] | None:
        for e in self._load():
            if e.get("id") == entry_id:
                return e
        return None

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        entries = self._load()
        if status:
            entries = [e for e in entries if e.get("status") == status]
        return entries

    def _set_decision(self, entry_id: str, status: str, reason: str) -> dict[str, Any] | None:
        entries = self._load()
        updated = None
        for e in entries:
            if e.get("id") == entry_id:
                e["status"] = status
                e["decided_at"] = _now()
                e["decision_reason"] = reason
                updated = e
                break
        if updated is not None:
            self._save(entries)
        return updated


#: The stable, real, importable contract a drafted test file uses to reach
#: its own not-yet-registered handler -- told to the model verbatim in
#: agent_smith's own tool description (general_roster.py) so generation
#: has one fixed target, not a guess at a future file layout.
LOAD_APPROVED_CONTRACT = (
    "from dourmouse.self_extensions import load_approved\n"
    "mod = load_approved(\"<tool_name>\")\n"
    "mod.handle({...})  # the real handler\n"
    "mod.TOOL_SPEC      # the real ToolSpec, permission is always REQUIRES_CONFIRMATION"
)


def load_approved(tool_name: str) -> types.ModuleType:
    """Dynamically imports an approved self-extension's real module file
    from the workspace (NOT the git-tracked package -- see the module
    docstring). This is the one real dynamic-import path in this whole
    codebase; used by: a drafted test file reaching its own handler
    (the contract above), the approval flow's own post-write sanity
    import, and general_roster.py's startup loader."""
    path = _approved_dir() / f"{tool_name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"no approved self-extension named {tool_name!r} at {path}")
    spec = importlib.util.spec_from_file_location(f"dourmouse_self_ext_{tool_name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load self-extension {tool_name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_approved_names() -> list[str]:
    d = _approved_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.py"))


def _write_approved_module(entry: dict[str, Any]) -> Path:
    """Writes the real, complete Python module for an approved tool.
    ALWAYS forces Permission.REQUIRES_CONFIRMATION (Domain D acceptance
    test 3: a self-added tool can never grant itself elevated access),
    regardless of anything the draft's own source claims."""
    approved_dir = _approved_dir()
    approved_dir.mkdir(parents=True, exist_ok=True)
    name = entry["tool_name"]
    confirm_prompt_body = entry["description"].replace('"', '\\"')
    module_source = (
        f'"""Self-extension {entry["id"]}: {entry["tool_name"]}.\n\n'
        f'Drafted by agent_smith for this capability gap:\n{entry["capability_gap"]}\n\n'
        f'Approved {_now()}. Full history: workspace/self_extensions/CHANGELOG.md.\n"""\n\n'
        "from dourmouse.dispatch import Permission, ToolSpec\n\n\n"
        f"{entry['handler_source']}\n\n\n"
        "TOOL_SPEC = ToolSpec(\n"
        f"    name={name!r},\n"
        f"    description={entry['description']!r},\n"
        f"    parameters={entry['parameters_schema']!r},\n"
        "    handler=handle,\n"
        "    permission=Permission.REQUIRES_CONFIRMATION,\n"
        f'    confirm_prompt=lambda a: "Run self-added tool {name}: {confirm_prompt_body}?",\n'
        ")\n"
    )
    module_path = approved_dir / f"{name}.py"
    module_path.write_text(module_source, encoding="utf-8")
    return module_path


def _write_and_run_test(entry: dict[str, Any]) -> tuple[bool, str]:
    """Writes the draft's own test file to the real dourmouse/tests/
    directory (pytest's own discovery root -- a loose file elsewhere
    would never actually run) and runs it, alone, in a real subprocess.
    Returns (passed, output). The test file is removed again on ANY
    outcome -- a self-extension's test lives in the changelog and in the
    approved module's own docstring reference, not as a permanent
    addition to this repository's own tracked test suite (that would mean
    every approval silently grows this repo's own git-tracked footprint
    with AI-authored test files no human reviewed as a commit)."""
    import dourmouse

    tests_dir = Path(dourmouse.__file__).resolve().parent / "tests"
    test_path = tests_dir / f"test_self_ext_{entry['tool_name']}.py"
    test_path.write_text(entry["test_source"], encoding="utf-8")
    subprocess_env = {**os.environ, _APPROVED_DIR_OVERRIDE_ENV: str(_approved_dir())}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-q"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
            cwd=str(Path(dourmouse.__file__).resolve().parent.parent),
            env=subprocess_env,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output[-4000:]
    except subprocess.TimeoutExpired:
        return False, "the draft's own test did not finish within 60 seconds"
    finally:
        test_path.unlink(missing_ok=True)


def _append_changelog(entry: dict[str, Any], changelog_path: Path | None = None) -> None:
    changelog = changelog_path or (workspace_dir() / _CHANGELOG_FILE)
    changelog.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Self-Extensions Changelog\n\n"
        "Every tool Dourmouse has ever added to itself (Domain D), in the order it was "
        "approved. A human approved every single line below -- see "
        "docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md Domain D for the review-gate design "
        "this changelog is part of.\n\n---\n\n"
    )
    if not changelog.is_file():
        changelog.write_text(header, encoding="utf-8")
    entry_text = (
        f"## {entry['tool_name']} ({entry['id']})\n\n"
        f"Approved: {entry['decided_at']}\n"
        f"Capability gap: {entry['capability_gap']}\n"
        f"Description: {entry['description']}\n"
        f"Goal: {entry['goal_id'] or '(created directly from chat, no goal)'}\n"
        "Permission: REQUIRES_CONFIRMATION (forced, regardless of the draft's own request)\n\n"
        "---\n\n"
    )
    with changelog.open("a", encoding="utf-8") as fh:
        fh.write(entry_text)


def approve(entry_id: str, *, store: SelfExtensions | None = None, changelog_path: Path | None = None) -> dict[str, Any]:
    """The one real approval path -- human-triggered only (webui.py's
    POST /api/self_extensions/approve), never a chat tool. Re-validates
    syntax, writes the real forced-REQUIRES_CONFIRMATION module, sanity-
    imports it, runs the draft's own real test in a subprocess, and only
    on a real pass marks it APPROVED and appends the permanent changelog.
    Any failure marks APPROVAL_FAILED with the real reason -- never
    silently merged."""
    store = store or SelfExtensions()
    entry = store.get(entry_id)
    if entry is None:
        return {"ok": False, "error": f"no such draft: {entry_id}"}
    if entry["status"] != "DRAFTED":
        return {"ok": False, "error": f"draft {entry_id} is already {entry['status']}, not DRAFTED"}
    syntax_error = check_syntax(entry["handler_source"])
    if syntax_error:
        store._set_decision(entry_id, "APPROVAL_FAILED", f"handler source: {syntax_error}")
        return {"ok": False, "error": f"handler source no longer parses: {syntax_error}"}
    from dourmouse.general_roster import build_general_registry

    existing = frozenset(build_general_registry().tool_names)
    name_error = validate_tool_name(entry["tool_name"], existing_names=existing)
    if name_error:
        store._set_decision(entry_id, "APPROVAL_FAILED", name_error)
        return {"ok": False, "error": name_error}
    try:
        _write_approved_module(entry)
        load_approved(entry["tool_name"])  # sanity import of code a human just approved
    except Exception as exc:  # noqa: BLE001 -- a broken draft must report honestly, never crash the approval route
        reason = f"{type(exc).__name__}: {exc}"
        store._set_decision(entry_id, "APPROVAL_FAILED", reason)
        return {"ok": False, "error": reason}
    passed, output = _write_and_run_test(entry)
    if not passed:
        reason = f"the draft's own test failed:\n{output}"
        store._set_decision(entry_id, "APPROVAL_FAILED", reason)
        (_approved_dir() / f"{entry['tool_name']}.py").unlink(missing_ok=True)
        return {"ok": False, "error": reason}
    updated = store._set_decision(entry_id, "APPROVED", "test passed, module written")
    _append_changelog(updated, changelog_path)
    return {
        "ok": True,
        "entry": updated,
        "note": "approved and merged. Restart the server for this tool to become callable.",
    }


def reject(entry_id: str, *, reason: str = "", store: SelfExtensions | None = None) -> dict[str, Any]:
    store = store or SelfExtensions()
    entry = store.get(entry_id)
    if entry is None:
        return {"ok": False, "error": f"no such draft: {entry_id}"}
    if entry["status"] != "DRAFTED":
        return {"ok": False, "error": f"draft {entry_id} is already {entry['status']}, not DRAFTED"}
    updated = store._set_decision(entry_id, "REJECTED", reason or "rejected, no reason given")
    return {"ok": True, "entry": updated}
