# UI Design References — Phase 3

What Claude Desktop, Claude Code, and Hermes-style agent interfaces get right,
why it works, and how Dourmouse adapts it. No proprietary assets, branding, or
exact layouts are reproduced here or in the implementation — these are
interaction patterns, described in our own words, built in Dourmouse's own
existing visual language (`ui/assets/dourmouse-ui.css`, see `docs/DESIGN_SYSTEM.md`).

## Claude Desktop

**Pattern: the conversation is the workspace, not one screen among many.**
Everything relevant to a turn — the model's text, a tool's activity, its
result, a follow-up — renders inline, in reading order, so the user never
leaves the thread to understand what happened. Why it works: it matches how
people already read a conversation; a separate "activity log" page forces a
context switch to answer "what did it just do."

*Dourmouse today*: `console.html`'s transcript already does this for
text/tool-chip pairs (`addReply()`/`act()`, `docs/UI_SOURCE_MAP.md` §5). The
gap is fidelity inside that inline stream — raw JSON tool arguments, no syntax
highlighting, no diff view — not the overall shape, which is already right.
*Adaptation*: keep everything inline; upgrade what renders inside the stream
(see the tool-activity and code-diff sections below), not the placement.

**Pattern: attachments and generated files are first-class, not links.**
A file the model produces or the user drops in shows a real preview
(thumbnail, page count, size) inline, with an obvious open action.

*Dourmouse today*: `ui/file_preview.html` exists as a separate page reached
via iframe, not an inline card. *Adaptation*: a compact `FileWidget` (name,
icon, size, operation) inline in the transcript, using `file_preview.html`'s
existing sandboxed viewer as the "open" target rather than replacing it.

## Claude Code

**Pattern: compact, expandable operation cards for file edits.** A change to
`research_engine.py` shows as a few lines — file name, `+18 −7`, a "View
changes" affordance — not the full diff dumped inline. Expanding reveals real
syntax-highlighted diff hunks. Why it works: most of the time the user only
needs to know *that* something changed and roughly how much; the full diff is
there on demand, not forced on every reader.

*Dourmouse today*: `edit_file`/`write_file`/`diff_preview` tools exist
server-side (`docs/UI_SOURCE_MAP.md` §5) but render through the same generic
`act()` chip as every other tool — no diff rendering anywhere in the repo.
This is the single highest-value net-new component for Phase 3: a
`DiffWidget` (compact `+N −N` summary, expand to real highlighted hunks),
built once and used by every file-editing tool's result.

**Pattern: terminal output is summarized, not flooded.** `pytest tests/security`
shows as "✓ 143 passed · 12.4s" with output available on demand, never
hundreds of raw lines inline by default.

*Dourmouse today*: `run_command`/`run_python` results render as raw `<pre>`
text of arbitrary length (capped at 8,000 characters, not summarized). A
`TerminalWidget` that parses a common shape (exit code, pass/fail counts when
detectable, duration) and falls back to the existing truncated-raw view when
it can't summarize is the concrete adaptation — never fabricate a summary
when the real shape isn't recognized.

**Pattern: tool status has a small, fixed vocabulary.** Queued, running,
completed, failed, cancelled — rendered the same way everywhere a tool runs,
so a user learns the vocabulary once. Why it works: consistency lets the eye
skim a long session instead of re-parsing each row's bespoke wording.

*Dourmouse today*: the `WORK` lookup table (`console.html`, ~110 entries)
already gives friendly per-tool labels, but status itself (running/done) is
conveyed only by the `.spin` class or presence of a result — no explicit
state vocabulary. *Adaptation*: a real `ToolActivity` component with the five
states named above, replacing the current chip, reusing `WORK`'s existing
label data rather than rewriting it.

## Hermes-style agent interfaces

**Pattern: dense information without visual noise.** Status, counts, and
metadata pack tightly (small type, tight rows, minimal chrome) rather than
using generous card padding for everything — density signals "engineering
tool," not "consumer app."

*Dourmouse today*: `dourmouse-ui.css`'s own stated philosophy already matches
this almost exactly (see its header: no floating cards, no translucent
washes, no decorative glow, `--dm-row-y: 6px` tight rows) — Phase 3 doesn't
need to invent this principle, only get `console.html`/`workspace.html` to
actually use the stylesheet that already states it (`docs/UI_SOURCE_MAP.md`
found 0 `dm-` class usages in either file today).

**Pattern: multi-agent activity as parallel, comparable streams.** Several
agents' progress shown as aligned rows (not separate pages per agent) so a
glance shows which is ahead, stalled, or done.

*Dourmouse today*: the ORCHESTRATION screen (`paintOrchestration()`,
`console.html:6538`) already does real parallel fan-out visualization for
`delegate_parallel` runs — this pattern is already implemented, not a gap.
*Adaptation for Phase 2's goals*: the same visual pattern, applied to a
Goal's task graph (`GET /api/goals?id=`) — parallel/dependent tasks as rows
with a state dot, not a new visual language.

## What we deliberately do NOT copy

Branding, color identity, exact card layouts, or proprietary iconography from
any of the above. Dourmouse's own dark, dense, terminal-adjacent look
(`dourmouse-ui.css`'s four solid layers, functional accent budget under ~10%
of the screen) is a real, considered, already-existing design language — Phase
3 extends it, it does not replace it with someone else's visual identity.
