"""The research network's object model (R1 + R2, finding #095). Pure: no I/O,
no clock, no model call, the same discipline as research_pipeline/core.py.

Spec item 35: "The database shouldn't simply contain conversations. It needs
first-class research objects." Twenty-one of them, and the relationships
between them are the point: "Hypothesis H1 supported by Evidence E1,
contradicted by Evidence E9, tested by Experiment X3, revised by Decision D7"
becomes a query instead of an inspection.

Spec item 36: SOURCE -> DOCUMENT -> PASSAGE -> EVIDENCE. "The interpretation
can change. The original passage should not." So each type declares its
mutability. IMMUTABLE objects have exactly one version, ever. VERSIONED
objects are never updated in place: a revision is a new version and the old
one stays readable, which is what answers "what did we actually know when this
conclusion was produced" (R2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Mutability(Enum):
    IMMUTABLE = "immutable"
    VERSIONED = "versioned"


@dataclass(frozen=True)
class ObjectSpec:
    name: str
    mutability: Mutability
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    #: At least one of these must be present (and non-empty).
    any_of: tuple[str, ...] = ()

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional) | frozenset(self.any_of)


_I, _V = Mutability.IMMUTABLE, Mutability.VERSIONED

#: The 21 first-class research objects of spec item 35.
OBJECT_TYPES: dict[str, ObjectSpec] = {s.name: s for s in (
    ObjectSpec("project", _V, ("name",), ("description", "status")),
    ObjectSpec("research_question", _V, ("text",), ("status",)),
    ObjectSpec("research_objective", _V, ("text",), ("status",)),
    ObjectSpec("hypothesis", _V, ("statement",), ("status", "rationale")),
    ObjectSpec("claim", _V, ("text",), (
        "status", "sub_question", "agent", "retrieved_at", "location", "legacy_source_id",
    )),
    ObjectSpec("source", _I, ("url",), ("title",)),
    # A document is identified by the SHA-256 of its stored raw bytes. Claims
    # made before finding #089 only ever hashed stripped text, and calling
    # that a raw hash would be false, so those carry legacy_text_hash instead.
    ObjectSpec("document", _I, (), (
        "requested_url", "final_url", "redirect_chain", "status", "content_type", "charset",
        "charset_source", "fetched_at", "raw_bytes", "truncated", "kind", "rendered",
        "rendered_from",
    ), any_of=("raw_sha256", "legacy_text_hash")),
    ObjectSpec("passage", _I, ("text",), ("location",)),
    ObjectSpec("evidence", _I, ("stance",), ("note",)),
    ObjectSpec("experiment", _V, ("protocol",), ("code", "status")),
    ObjectSpec("experiment_run", _V, ("status",), (
        "environment_hash", "node", "started_at", "finished_at", "exit_code", "logs",
    )),
    ObjectSpec("dataset", _I, ("name",), ("uri", "sha256", "description")),
    ObjectSpec("metric", _I, ("name", "value"), ("unit",)),
    ObjectSpec("result", _I, ("summary",), ("data",)),
    ObjectSpec("contradiction", _V, ("note",), ("sub_question", "status")),
    ObjectSpec("agent", _V, ("name",), ("role",)),
    ObjectSpec("task", _V, ("title", "stage"), ("status",)),
    ObjectSpec("message", _I, ("body",), ("from_agent", "to_agent")),
    ObjectSpec("decision", _I, ("summary",), ("rationale",)),
    ObjectSpec("artifact", _I, ("kind",), ("uri", "sha256")),
    ObjectSpec("event", _I, ("type",), ("payload",)),
)}

#: Typed relations. A closed vocabulary: an edge the model cannot name is a
#: bug to surface, not a free-text label to accumulate.
RELATIONS: frozenset[str] = frozenset({
    "part_of",          # passage part_of document, question part_of project
    "derived_from",     # document derived_from source; rendered DOM from server bytes
    "extracted_from",   # evidence extracted_from passage
    "supported_by",     # claim/hypothesis supported_by evidence
    "contradicted_by",  # claim/hypothesis contradicted_by evidence or claim
    "tested_by",        # hypothesis tested_by experiment
    "revised_by",       # hypothesis/claim revised_by decision
    "decomposes_into",  # question decomposes_into sub-question
    "answers",          # claim/result answers question
    "about",            # contradiction/task/result about an object
    "spawned",          # contradiction spawned task (the backward edge, R3)
    "assigned_to",      # task assigned_to agent
    "produced",         # experiment_run produced result/metric/artifact
    "run_of",           # experiment_run run_of experiment
    "uses",             # experiment uses dataset
    "replicates",       # experiment_run replicates experiment_run
    "cites",            # document/result cites source
    "sent",             # agent sent message
})


class GraphError(ValueError):
    pass


class ImmutableObject(GraphError):
    pass


def validate_body(obj_type: str, body: dict[str, Any], *, partial: bool = False) -> None:
    """Every required field present (unless ``partial``, for a revision's
    changes), and no field the type does not declare: a typo'd field name
    would otherwise be stored and silently never read."""
    spec = OBJECT_TYPES.get(obj_type)
    if spec is None:
        raise GraphError(f"unknown object type {obj_type!r}")
    unknown = set(body) - spec.fields
    if unknown:
        raise GraphError(f"{obj_type} has no field(s) {sorted(unknown)}")
    if not partial:
        missing = [f for f in spec.required if body.get(f) in (None, "")]
        if missing:
            raise GraphError(f"{obj_type} is missing required field(s) {missing}")
        if spec.any_of and all(body.get(f) in (None, "") for f in spec.any_of):
            raise GraphError(f"{obj_type} needs at least one of {list(spec.any_of)}")


def validate_relation(relation: str) -> None:
    if relation not in RELATIONS:
        raise GraphError(f"unknown relation {relation!r}")
