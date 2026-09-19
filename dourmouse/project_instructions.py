"""Project-instruction file: Dourmouse's own equivalent of Claude Code's
CLAUDE.md (Domain H, docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md).

Answers this codebase's own previously-open question, confirmed by grep
before this module existed: no such mechanism existed anywhere in
dourmouse/. This is the smallest real piece of Domain H's own build plan --
one file, one workspace-root path, spliced into the SAME `system_message()`
every backend already shares (dispatch.py), the same "alongside, not
instead of" pattern `agent_prompts.py`'s own bespoke per-agent prompts
already use for exactly this reason: the base orchestrator rules (
confirmation-gating, honest failure, no fabrication) must never be dropped
just because a user wrote their own standing instructions.

Per-directory nesting (a narrower file in a subfolder overriding/extending
the workspace-root one, matching CLAUDE.md's own nearest-file-wins
convention) is real, separate, deliberately NOT built here -- it needs a
real `cwd` threaded into `system_message()`'s own signature, which every
current caller (chat.ChatSession, run_dispatch, delegate_task) would need
updating for, a much larger surface than "read one well-known file."
"""

from __future__ import annotations

from dourmouse.config import workspace_dir

# Same bounding discipline as general_roster.py's _DELEGATE_RESULT_CAP and
# research_mesh/brain.py's _STUDY_CONTEXT_CAP_CHARS -- a real file the user
# actually wrote is bounded, but nothing stops someone pasting a huge
# document in by mistake, and this rides on EVERY turn's system prompt.
_PROJECT_INSTRUCTIONS_CAP_CHARS = 8_000

PROJECT_INSTRUCTIONS_FILENAME = "DOURMOUSE.md"


def load_project_instructions() -> str:
    """The real, current content of <workspace>/DOURMOUSE.md, capped and
    stripped, or "" when missing/unreadable/empty -- never fabricated, and
    an honest empty string (not a placeholder) is exactly what lets
    system_message() stay byte-identical to before this module existed
    when the user has not written one."""
    path = workspace_dir() / PROJECT_INSTRUCTIONS_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > _PROJECT_INSTRUCTIONS_CAP_CHARS:
        text = text[:_PROJECT_INSTRUCTIONS_CAP_CHARS] + "\n[project instructions truncated]"
    return text
