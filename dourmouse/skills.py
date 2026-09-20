"""Skills-as-modular-capability-packages (Domain H, piece 3 of 7).

A ``dourmouse/skills/<name>/SKILL.md`` convention: a skill is real,
human-authorable documentation (frontmatter + markdown body), never a giant
block concatenated into the always-on system prompt. It is loaded into a
turn's context ONLY when that turn's own text looks relevant to it --
deterministic keyword-overlap matching (Rule 2.8: no LLM judgment in the
lookup path), the same discipline ``planner.find_agents_for_query`` already
established for routing a turn to the right SUBAGENT. This is that same
idea applied to a capability PACKAGE instead of a registered agent, not a
second relevance-scoring system.

``SKILL.md`` format (deliberately minimal, no YAML dependency -- this
codebase has none today and a skill file is simple enough not to need one):

    ---
    name: skill-name
    description: one line, shown in tool/skill listings
    keywords: comma, separated, trigger, words
    ---
    <markdown body -- instructions, examples, whatever the skill needs>

The harsh acceptance test this piece is built against (per the founding
spec): an external developer should be able to write a new skill from this
docstring and the format above alone, without reading any of this module's
own implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Real, in-repo location -- skills are developer-authored capability
#: packages shipped with Dourmouse, not per-workspace user data (unlike
#: e.g. memory_store's workspace-relative DEFAULT_DB).
SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    keywords: tuple[str, ...]
    body: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md's leading ``---``-delimited frontmatter from its
    body. Returns ``({}, text)`` unchanged when the file has no
    frontmatter at all -- a malformed skill file is skipped by the caller,
    never guessed at."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
        i += 1
    body = "\n".join(lines[i + 1 :]).strip() if i < len(lines) else ""
    return fields, body


def _load_one(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, body = _parse_frontmatter(text)
    name = fields.get("name", "").strip()
    if not name:
        return None  # a skill with no real name is not a real skill
    keywords = tuple(
        k.strip().lower() for k in fields.get("keywords", "").split(",") if k.strip()
    )
    return Skill(
        name=name,
        description=fields.get("description", "").strip(),
        keywords=keywords,
        body=body,
        path=path,
    )


def load_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Every real, well-formed skill under ``skills_dir`` (default
    ``SKILLS_DIR``) -- one subdirectory per skill, each holding its own
    ``SKILL.md``. A missing directory returns an empty list, not an error
    (a fresh checkout with no skills yet is a normal state, not a bug).
    Sorted by name for deterministic ordering."""
    d = skills_dir or SKILLS_DIR
    if not d.is_dir():
        return []
    out = []
    for sub in sorted(d.iterdir()):
        md = sub / "SKILL.md"
        if sub.is_dir() and md.is_file():
            skill = _load_one(md)
            if skill is not None:
                out.append(skill)
    return sorted(out, key=lambda s: s.name)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", text.lower()))


def relevant_skills(prompt: str, skills: list[Skill] | None = None) -> list[Skill]:
    """Deterministic keyword-overlap relevance: a skill is relevant when at
    least one of its own declared ``keywords`` appears as a whole token in
    ``prompt``. No fuzzy matching, no LLM judgment -- a skill with keywords
    that never appear in a turn's text is never loaded for that turn.
    Sorted by overlap count (most-relevant first), then name, for
    deterministic output on a tie."""
    if skills is None:
        skills = load_skills()
    tokens = _tokenize(prompt)
    scored = [
        (len(tokens & set(skill.keywords)), skill)
        for skill in skills
        if tokens & set(skill.keywords)
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [skill for _, skill in scored]


def skill_context_block(prompt: str, skills: list[Skill] | None = None) -> str:
    """The real text to splice into a turn's context -- an empty string
    when nothing is relevant (the whole point of this piece: never a
    giant always-on system-prompt block). One ``[SKILL: name]`` section
    per matched skill, most-relevant first."""
    matched = relevant_skills(prompt, skills)
    if not matched:
        return ""
    return "\n\n".join(f"[SKILL: {s.name}]\n{s.body}" for s in matched)
