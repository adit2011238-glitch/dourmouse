"""3D & UI Design asset spec generation + manifest cataloguing (v1.0).

Real, deterministic tools (Rule 2.8 — no model in the loop; Rule 2.2 — no
silent stubs) for the ``design_3d`` roster subagent, built to eventually
serve the barely-populated scaffold at ``D:\\spatial_ai_library\\`` on the
desktop (``ui_components\\ui_manifest.json``, ``3d_models\\``). This module
runs on whatever machine hosts Dourmouse (Mac dev box today) and never
assumes a ``D:\\`` path exists — every path is resolved at call time via
the manifest-path convention below.

WHAT THIS ACTUALLY IS (say it plainly, every time): a spec-generation and
cataloguing tool for 3D/UI design ASSETS, described at the level a human
designer would sketch on a whiteboard — component dimensions/color/opacity,
or a 3D model as a composition of named primitives (box/sphere/cylinder/
...) with position, scale and a material description. It is NOT a 3D
renderer, NOT a CAD engine, and NOT a mesh generator: no vertex/face/UV
geometry and no real 3D asset file (.obj/.glb/.fbx/.stl) is ever produced.
A real next step — e.g. a Three.js frontend that actually renders these
specs, or a dedicated mesh-generation pipeline (headless Blender/CAD
backend, a mesh-generation API) — is separate future work; every tool
description below says so again at the point it matters.

Manifest path resolution (CONFIGURABLE, three layers, checked in order):
  1. an explicit ``manifest_path`` argument on the tool call itself
  2. the ``DOURMOUSE_UI_MANIFEST_PATH`` env var (process-wide override —
     on the eventual desktop deployment this would point at the real
     ``D:\\spatial_ai_library\\ui_components\\ui_manifest.json``)
  3. the default: ``<DOURMOUSE_WORKSPACE>/design_3d/ui_manifest.json``
     (``DOURMOUSE_WORKSPACE`` falls back to a relative ``workspace`` dir
     when unset) — same env-var convention every other module in this
     codebase uses (see ``world_watch_regions.py``, ``live_feeds.py``,
     ``general_roster.py``'s own ``_workspace_root``).

The manifest file itself is the exact shape the desktop scaffold already
uses: a flat JSON object of ``name -> {category, description,
dimensions: {width, height}, color, opacity}``. Nothing here invents a
different shape or a separate schema for 3D models — no manifest format
was specified for ``3d_models\\`` (it is currently empty), so
``generate_3d_model_spec`` only ever returns a spec for the caller to
review; it does not write one anywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_MANIFEST_PATH_ENV = "DOURMOUSE_UI_MANIFEST_PATH"
_DEFAULT_MANIFEST_RELPATH = Path("design_3d") / "ui_manifest.json"

_VALID_PRIMITIVES = {"box", "sphere", "cylinder", "cone", "plane", "torus"}


# --------------------------------------------------------------------------- #
# Manifest path + I/O
# --------------------------------------------------------------------------- #

def _manifest_path(arguments: dict[str, Any]) -> Path:
    """Resolve the manifest JSON path (CONFIGURABLE — see module docstring)."""
    explicit = str(arguments.get("manifest_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(_MANIFEST_PATH_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    root = Path(os.environ.get("DOURMOUSE_WORKSPACE", "").strip() or "workspace")
    return (root / _DEFAULT_MANIFEST_RELPATH).expanduser()


def _load_manifest(path: Path) -> dict[str, Any]:
    """Load the manifest JSON object, or {} when the file doesn't exist yet.

    Raises ValueError (never silently swallowed — Rule 2.2) when the file
    exists but is not valid JSON, or is not an object keyed by name.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"manifest at {path} must be a JSON object of name -> entry, "
            f"got {type(data).__name__}"
        )
    return data


# --------------------------------------------------------------------------- #
# UI component spec — validated, matches ui_manifest.json's exact shape
# --------------------------------------------------------------------------- #

def _build_component_entry(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate + build ONE manifest entry. Raises ValueError with a plain
    human-readable reason on any bad input — never a silent default that
    papers over a missing/malformed field."""
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ValueError("requires a non-empty 'name'.")
    category = str(arguments.get("category") or "").strip()
    if not category:
        raise ValueError("requires a non-empty 'category'.")
    description = str(arguments.get("description") or "").strip()
    if not description:
        raise ValueError("requires a non-empty 'description'.")
    if arguments.get("width") is None or arguments.get("height") is None:
        raise ValueError("requires 'width' and 'height' (numbers).")
    try:
        width = float(arguments["width"])
        height = float(arguments["height"])
    except (TypeError, ValueError):
        raise ValueError("'width' and 'height' must be numbers.")
    if width <= 0 or height <= 0:
        raise ValueError("'width' and 'height' must be positive numbers.")
    color = str(arguments.get("color") or "#888888").strip() or "#888888"
    try:
        opacity = float(arguments.get("opacity", 1.0))
    except (TypeError, ValueError):
        raise ValueError("'opacity' must be a number between 0 and 1.")
    if not (0.0 <= opacity <= 1.0):
        raise ValueError("'opacity' must be between 0 and 1.")
    entry = {
        "category": category,
        "description": description,
        "dimensions": {"width": width, "height": height},
        "color": color,
        "opacity": opacity,
    }
    return name, entry


def _generate_ui_component_spec_tool(arguments: dict[str, Any]) -> str:
    try:
        name, entry = _build_component_entry(arguments)
    except ValueError as exc:
        return f"ERROR: generate_ui_component_spec {exc}"
    return (
        "UI COMPONENT SPEC (generated only — nothing written yet; call "
        "write_manifest_entry to persist it):\n" + json.dumps({name: entry}, indent=2)
    )


# --------------------------------------------------------------------------- #
# 3D model spec — primitive-composition level ONLY (explicitly not a mesh)
# --------------------------------------------------------------------------- #

def _generate_3d_model_spec_tool(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "").strip()
    if not name:
        return "ERROR: generate_3d_model_spec requires a non-empty 'name'."
    raw_primitives = arguments.get("primitives")
    if not isinstance(raw_primitives, list) or not raw_primitives:
        return (
            "ERROR: generate_3d_model_spec requires a non-empty 'primitives' "
            "array — each item describes ONE primitive shape "
            "(type/position/scale/material)."
        )
    normalized: list[dict[str, Any]] = []
    for i, p in enumerate(raw_primitives):
        if not isinstance(p, dict):
            return f"ERROR: primitives[{i}] must be an object."
        ptype = str(p.get("type") or "").strip().lower()
        if ptype not in _VALID_PRIMITIVES:
            return (
                f"ERROR: primitives[{i}].type {ptype!r} is not one of the "
                f"supported primitive shapes: {sorted(_VALID_PRIMITIVES)}."
            )
        pos = p.get("position", [0, 0, 0])
        if not (isinstance(pos, list) and len(pos) == 3
                and all(isinstance(v, (int, float)) for v in pos)):
            return f"ERROR: primitives[{i}].position must be [x, y, z] numbers."
        scale = p.get("scale", [1, 1, 1])
        if isinstance(scale, (int, float)):
            scale = [scale, scale, scale]
        if not (isinstance(scale, list) and len(scale) == 3
                and all(isinstance(v, (int, float)) for v in scale)):
            return (
                f"ERROR: primitives[{i}].scale must be [x, y, z] numbers "
                "(or a single number for uniform scale)."
            )
        material = p.get("material") or {}
        if not isinstance(material, dict):
            return (
                f"ERROR: primitives[{i}].material must be an object, e.g. "
                "{'color': '#8899aa'}."
            )
        normalized.append({
            "type": ptype,
            "position": [float(v) for v in pos],
            "scale": [float(v) for v in scale],
            "material": {
                "color": str(material.get("color", "#888888")),
                "roughness": float(material.get("roughness", 0.5)),
            },
        })
    spec = {
        "name": name,
        "description": str(arguments.get("description") or "").strip(),
        "primitives": normalized,
    }
    return (
        "3D MODEL SPEC (primitive-composition level — position/scale/"
        "material only). This is NOT real mesh/CAD geometry: no vertices, "
        "faces, UVs, or an actual 3D asset file (.obj/.glb/.fbx/.stl) are "
        "produced. Real mesh/CAD generation is OUT OF SCOPE for this tool "
        "and would need a dedicated pipeline (e.g. a headless Blender/CAD "
        "backend, or a mesh-generation API) as separate future work. This "
        "spec is also NOT written to any manifest — there is no manifest "
        "schema defined yet for 3d_models\\.\n" + json.dumps(spec, indent=2)
    )


# --------------------------------------------------------------------------- #
# Manifest read/list/write
# --------------------------------------------------------------------------- #

def _list_manifest_tool(arguments: dict[str, Any]) -> str:
    path = _manifest_path(arguments)
    if not path.exists():
        return (
            f"MANIFEST: no file at {path} yet (honest — nothing has been "
            "written here). Pass 'manifest_path' to point at an existing "
            "one, or call write_manifest_entry to create it."
        )
    try:
        data = _load_manifest(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not data:
        return f"MANIFEST ({path}): 0 entries."
    lines = [f"MANIFEST ({path}): {len(data)} entries"]
    for name, entry in sorted(data.items()):
        if not isinstance(entry, dict):
            lines.append(f"- {name}: (malformed entry — not an object)")
            continue
        dims = entry.get("dimensions") or {}
        lines.append(
            f"- {name}: category={entry.get('category', '?')} "
            f"size={dims.get('width', '?')}x{dims.get('height', '?')} "
            f"color={entry.get('color', '?')} opacity={entry.get('opacity', '?')}"
        )
    return "\n".join(lines)


def _read_manifest_entry_tool(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "").strip()
    if not name:
        return "ERROR: read_manifest_entry requires a non-empty 'name'."
    path = _manifest_path(arguments)
    if not path.exists():
        return f"ERROR: no manifest file at {path}."
    try:
        data = _load_manifest(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    entry = data.get(name)
    if entry is None:
        known = ", ".join(sorted(data)) or "(none)"
        return f"ERROR: no entry named {name!r} in manifest at {path}. Known: {known}"
    return json.dumps({name: entry}, indent=2)


def _write_manifest_confirm_prompt(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "?").strip() or "?"
    path = _manifest_path(arguments)
    try:
        existed = name in _load_manifest(path)
    except ValueError:
        existed = False
    verb = "OVERWRITE the existing" if existed else "ADD a new"
    return (
        f"{verb} entry {name!r} in the UI manifest at {path}? This writes "
        "the component spec (category/description/dimensions/color/"
        "opacity) to that JSON file."
    )


def _write_manifest_entry_tool(arguments: dict[str, Any]) -> str:
    try:
        name, entry = _build_component_entry(arguments)
    except ValueError as exc:
        return f"ERROR: write_manifest_entry {exc}"
    path = _manifest_path(arguments)
    try:
        data = _load_manifest(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    existed = name in data
    data[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verb = "UPDATED (overwrote existing)" if existed else "ADDED"
    return f"{verb} manifest entry {name!r} at {path}:\n" + json.dumps({name: entry}, indent=2)


# --------------------------------------------------------------------------- #
# Roster wiring
# --------------------------------------------------------------------------- #

def build_design_3d_tool_specs() -> list[Any]:
    """ToolSpecs for the ``design_3d`` subagent."""
    from dourmouse.dispatch import Permission, ToolSpec

    return [
        ToolSpec(
            name="generate_ui_component_spec",
            description=(
                "Generate/describe a UI component spec matching the desktop "
                "ui_manifest.json shape EXACTLY: {category, description, "
                "dimensions: {width, height}, color, opacity}. Deterministic "
                "validation, no model in the loop. Returns the spec as a "
                "preview — call write_manifest_entry to actually persist it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "width": {"type": "number"},
                    "height": {"type": "number"},
                    "color": {"type": "string", "default": "#888888"},
                    "opacity": {"type": "number", "default": 1.0},
                },
                "required": ["name", "category", "description", "width", "height"],
            },
            handler=_generate_ui_component_spec_tool,
        ),
        ToolSpec(
            name="generate_3d_model_spec",
            description=(
                "Generate/describe a simple 3D model spec at PRIMITIVE-"
                "COMPOSITION level: a list of primitives "
                "(box/sphere/cylinder/cone/plane/torus), each with a "
                "position [x,y,z], scale [x,y,z] (or one number for "
                "uniform scale), and a material description (color, "
                "roughness). This does NOT generate a real mesh/CAD asset — "
                "no vertices/faces/UVs, no .obj/.glb/.fbx/.stl output. Real "
                "mesh/CAD generation is out of scope and would need a "
                "dedicated pipeline as separate future work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "primitives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": sorted(_VALID_PRIMITIVES),
                                },
                                "position": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                                "scale": {
                                    "description": "[x,y,z] or a single number for uniform scale",
                                },
                                "material": {
                                    "type": "object",
                                    "properties": {
                                        "color": {"type": "string"},
                                        "roughness": {"type": "number"},
                                    },
                                },
                            },
                            "required": ["type"],
                        },
                    },
                },
                "required": ["name", "primitives"],
            },
            handler=_generate_3d_model_spec_tool,
        ),
        ToolSpec(
            name="list_manifest",
            description=(
                "List every entry in the UI manifest (name, category, "
                "size, color, opacity). Reads the manifest at 'manifest_path' "
                "if given, else DOURMOUSE_UI_MANIFEST_PATH, else the "
                "default design_3d/ui_manifest.json location. Honestly "
                "reports when nothing has been catalogued yet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "manifest_path": {"type": "string"},
                },
            },
            handler=_list_manifest_tool,
        ),
        ToolSpec(
            name="read_manifest_entry",
            description="Read ONE entry from the UI manifest by name.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "manifest_path": {"type": "string"},
                },
                "required": ["name"],
            },
            handler=_read_manifest_entry_tool,
        ),
        ToolSpec(
            name="write_manifest_entry",
            description=(
                "Write (add, or overwrite if the same name already exists) "
                "ONE component entry into the UI manifest JSON — the same "
                "shape generate_ui_component_spec produces. REQUIRES human "
                "confirmation: this can silently overwrite an existing "
                "entry with no diff shown."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "width": {"type": "number"},
                    "height": {"type": "number"},
                    "color": {"type": "string", "default": "#888888"},
                    "opacity": {"type": "number", "default": 1.0},
                    "manifest_path": {"type": "string"},
                },
                "required": ["name", "category", "description", "width", "height"],
            },
            handler=_write_manifest_entry_tool,
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=_write_manifest_confirm_prompt,
        ),
    ]
