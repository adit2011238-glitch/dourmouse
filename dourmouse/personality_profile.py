"""One-time working-style profile, generated from imported history.

Explicit user spec: run ONCE, at setup — never on a schedule, never
silently regenerated. Covers working style, preferences, communication
preferences, general interests, and general context on working-style
preferences. Explicitly NOT biographical data (name, age, location,
employer, relationships, or other identifying personal details) — the
source material is a coding/research assistant's session history, which
is mostly technical, but the instruction is explicit and enforced both
in the prompt and structurally (the output schema below has no field
that COULD hold a name or an address).

Unlike history_import.py (a mechanical file parse, "no API needed" by
design), this genuinely needs an LLM: turning a pile of session
summaries into "prefers terse answers, iterates fast, dislikes
over-explaining" is a real interpretive judgment, not something a
keyword filter can do. One call, ever, by design — this is not a
per-session summarizer.
"""

from __future__ import annotations

import json
from typing import Any

from dourmouse.config import NvidiaConfig, OllamaConfig, load_llm_config
from dourmouse.memory_store import MemoryStore

PROFILE_SOURCE = "user_profile"
PROFILE_TITLE = "Working style & preferences"

# Enough source material for a real read without an unbounded prompt --
# ~20k chars is comfortably inside every backend's context window even
# stacked with the rest of a normal system prompt, and history_import's
# facts are already short structured summaries (title + a few lines),
# not raw transcripts, so this comfortably covers dozens of sessions.
_MAX_SOURCE_CHARS = 20_000

_SYSTEM_PROMPT = (
    "You build a working-style profile from a user's own past session "
    "summaries with AI coding/research assistants. Cover ONLY: working "
    "style, preferences, communication preferences, general interests, "
    "and general working-style context. \n\n"
    "STRICT EXCLUSION, never violate this: no biographical or "
    "identifying data of any kind — no name, age, location, employer, "
    "job title, relationships, or any other personal identifying "
    "detail, even if the source material contains one. If a session "
    "summary names a person, project client, or place, describe the "
    "PATTERN it implies (e.g. \"works across multiple concurrent "
    "projects\") and drop the specific identifying detail entirely. "
    "When in doubt, leave it out — a thinner profile is fine, a leaked "
    "identifying detail is not.\n\n"
    "Write plain prose in four short paragraphs, one per category "
    "below, each starting with its label. No bullet lists, no "
    "preamble, no meta-commentary about the task itself."
)

_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "working_style": {"type": "string"},
        "preferences": {"type": "string"},
        "communication_preferences": {"type": "string"},
        "general_interests": {"type": "string"},
    },
    "required": [
        "working_style", "preferences", "communication_preferences", "general_interests",
    ],
}


def has_profile(store: MemoryStore) -> bool:
    return store.get(PROFILE_SOURCE, PROFILE_TITLE) is not None


def _gather_source_material(store: MemoryStore, max_chars: int = _MAX_SOURCE_CHARS) -> str:
    """Concatenated title+body of every imported history fact, capped.

    Deliberately reads from BOTH claude_history and codex_history (see
    history_import.py) — session summaries, not raw transcripts, so this
    is already a mechanically-filtered, structured source (orchestration
    noise already excluded there).
    """
    facts = [
        f for f in store.all_facts()
        if f["source"] in ("claude_history", "codex_history")
    ]
    parts = []
    total = 0
    for f in facts:
        chunk = f"### {f['title']}\n{f['body']}\n"
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def _build_client(config: NvidiaConfig | OllamaConfig) -> Any:
    if isinstance(config, OllamaConfig):
        from dourmouse.dispatch import OllamaNativeClient

        return OllamaNativeClient(config)
    from openai import OpenAI

    return OpenAI(api_key=config.api_key or "local-keyless", base_url=config.base_url)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first {...} JSON object out of a completion. Models
    sometimes wrap JSON in prose or a code fence despite instructions;
    this is deliberately tolerant rather than requiring a byte-exact
    response, matching the honesty convention elsewhere in this codebase
    (degrade gracefully, never fabricate)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def generate_profile(
    store: MemoryStore,
    client: Any | None = None,
    config: NvidiaConfig | OllamaConfig | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate the one-time profile. Returns {"ok": bool, ...}.

    ``force`` exists only for an explicit, deliberate re-run (e.g. the
    user asks to redo it) — the default is a hard "already done" refusal,
    matching the "once, at setup, never on a schedule" spec exactly.
    """
    if has_profile(store) and not force:
        return {"ok": False, "reason": "already_generated"}

    source_material = _gather_source_material(store)
    if not source_material.strip():
        return {"ok": False, "reason": "no_source_material"}

    if client is None:
        config = config or load_llm_config()
        client = _build_client(config)
    model = getattr(config, "model", None) or "test-model"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Session summaries:\n\n" + source_material +
                "\n\nRespond with ONLY a JSON object matching this shape "
                "(four string fields, plain prose paragraphs, no nested "
                "objects): " + json.dumps(_PROFILE_SCHEMA["properties"])
            ),
        },
    ]
    try:
        # v8.14: 900 was traced live and is nowhere near enough — this
        # backend (nvidia/nemotron) visibly reasons in `content` before
        # answering (the same behavior chased down earlier for the
        # essay-length problem elsewhere in this codebase). At 900 tokens
        # the cap cut it off mid-reasoning ("Now produce JSON... Let's
        # craft: Working style: \"Working style: The user tends to...")
        # and the honest-degrade fallback below stored that scratchpad
        # as if it were the answer — genuinely on-topic content, just
        # never finished. This is a single one-time call, not a hot
        # path, so there is no real cost reason to cap it tight.
        response = client.chat.completions.create(
            model=model, messages=messages, max_tokens=4000,
        )
    except Exception as exc:  # honest failure surface (Rule 2.2)
        return {"ok": False, "reason": f"llm_call_failed: {exc}"}

    raw = (response.choices[0].message.content or "").strip()
    parsed = _extract_json(raw)
    if parsed is None or not all(k in parsed for k in _PROFILE_SCHEMA["required"]):
        # Honest degrade: store the raw prose rather than fabricate
        # structure that was never actually returned.
        body = raw or "(model returned no content)"
    else:
        body = (
            f"Working style: {parsed['working_style']}\n\n"
            f"Preferences: {parsed['preferences']}\n\n"
            f"Communication preferences: {parsed['communication_preferences']}\n\n"
            f"General interests: {parsed['general_interests']}"
        )

    store.remember(PROFILE_SOURCE, PROFILE_TITLE, body)
    return {"ok": True, "profile": body}
