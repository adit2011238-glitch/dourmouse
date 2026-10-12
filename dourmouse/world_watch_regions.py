"""World Watch Regions — user-drawn alert boxes for the World Pulse map (v1.0).

A person draws a rectangle on the world map (the one served by
``world_pulse.world_pulse_geo()``) and wants to be told only when a REAL
located monitor item — an earthquake, a disaster alert, a tracked flight,
anything carrying genuine ``lat``/``lon`` — falls inside that box. Nothing
else: a region with nothing in it is honestly empty, never padded with a
plausible-sounding "nearby" item (Rule 2.2 — never fabricate).

Two responsibilities, both deterministic and LLM-free (Rule 2.8):

- CRUD for named rectangular regions, persisted as a flat JSON list under
  the workspace — the exact file-shape/style ``live_feeds._load_tasks`` /
  ``live_feeds._save_tasks`` use for the task list, just pointed at
  ``watch_regions.json`` instead of ``tasks.json``. Same env-var
  convention too: ``DOURMOUSE_WORKSPACE`` picks the workspace root, and
  ``DOURMOUSE_WATCH_REGIONS_FILE`` (mirroring ``DOURMOUSE_TASKS_FILE``)
  overrides the file path outright.
- ``check_region_hits`` — given a ``world_pulse_geo()``-shaped snapshot,
  a pure point-in-rectangle test (inclusive bounds) against every
  persisted region, across every layer the snapshot carries. An item is
  only ever counted as a hit when it carries real numeric coordinates;
  a malformed or empty snapshot yields zero hits everywhere, never an
  exception — callers poll this on a timer and a bad snapshot must not
  take the polling loop down with it.

No network calls live in this module at all — it only ever reads the geo
dict handed to it and the region file it owns.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.state_file import StateUnreadable, atomic_write_text, load_json_or_aside

#: Serialises the read-modify-write of the region file within this process.
_REGIONS_LOCK = threading.RLock()

_WATCH_REGIONS_ENV = "DOURMOUSE_WATCH_REGIONS_FILE"

_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)


# --------------------------------------------------------------------------- #
# Storage — mirrors live_feeds._tasks_path / _load_tasks / _save_tasks
# --------------------------------------------------------------------------- #

def _regions_path() -> Path:
    """Resolve the region file path exactly the way tasks.json is resolved.

    Copied from ``live_feeds._tasks_path()``: ``DOURMOUSE_WORKSPACE`` (falling
    back to a relative ``workspace`` dir) picks the root the file lives
    under, and ``DOURMOUSE_WATCH_REGIONS_FILE`` — the watch-regions analogue
    of ``DOURMOUSE_TASKS_FILE`` — overrides the path outright when set.
    """
    # workspace_dir(), not a cwd-relative "workspace" (finding #090): a
    # process started from another directory read and wrote a different file.
    from dourmouse.config import workspace_dir

    root = workspace_dir()
    env = os.environ.get(_WATCH_REGIONS_ENV, "").strip()
    if env:
        return Path(env)
    return root / "watch_regions.json"


def _regions_file() -> Path:
    return _regions_path().expanduser()


def _load_regions(path: Path) -> list[dict[str, Any]]:
    """The stored regions for READING: a missing, unreadable or malformed file is an empty list. Reading
    never moves or changes the file; writers use ``_load_regions_for_update`` instead."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _load_regions_for_update(path: Path) -> list[dict[str, Any]]:
    """The stored regions for a caller that is about to WRITE. A missing file is an empty list. A file that
    was read but is bad JSON or not a list is kept aside (``<name>.corrupt-<time>``, never overwritten) and
    the change is refused with a message saying where it went; the next call starts a new list. A file that
    cannot be read right now (permission, I/O) is left exactly where it is and the change is refused.
    Returning [] here would let the write that follows put an empty list over every region."""
    try:
        value, note = load_json_or_aside(path, list)
    except StateUnreadable as exc:
        raise RuntimeError(f"{exc} The region was not saved; try again later.") from exc
    if note:
        raise RuntimeError(f"{note} The change was not saved; try again to start a new list.")
    return [r for r in (value or []) if isinstance(r, dict)]


def _save_regions(path: Path, regions: list[dict[str, Any]]) -> None:
    atomic_write_text(path, json.dumps(regions, indent=2, ensure_ascii=False) + "\n")


def _next_region_id(regions: list[dict[str, Any]]) -> str:
    """region-<max numeric suffix + 1>: len(regions)+1 collides as soon as one region has been deleted."""
    highest = 0
    for r in regions:
        m = re.fullmatch(r"region-(\d+)", str(r.get("id", "")))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"region-{highest + 1}"


# --------------------------------------------------------------------------- #
# Validation — honest, specific errors; never clamp or guess
# --------------------------------------------------------------------------- #

def _as_float(value: Any, field: str) -> float:
    """Parse one coordinate field, or raise a ValueError naming the field.

    Deliberately does not clamp or coerce a bad value into something
    plausible — an unparseable coordinate is a caller bug that must be
    reported, not silently rounded into range.
    """
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a real number, got {value!r}") from exc


def _validate_region(
    name: str, min_lat: float, max_lat: float, min_lon: float, max_lon: float
) -> tuple[str, float, float, float, float]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("add_region requires a non-empty name")

    lo_lat = _as_float(min_lat, "min_lat")
    hi_lat = _as_float(max_lat, "max_lat")
    lo_lon = _as_float(min_lon, "min_lon")
    hi_lon = _as_float(max_lon, "max_lon")

    lat_lo, lat_hi = _LAT_RANGE
    lon_lo, lon_hi = _LON_RANGE
    if not (lat_lo <= lo_lat <= lat_hi):
        raise ValueError(f"min_lat must be between {lat_lo} and {lat_hi}, got {lo_lat}")
    if not (lat_lo <= hi_lat <= lat_hi):
        raise ValueError(f"max_lat must be between {lat_lo} and {lat_hi}, got {hi_lat}")
    if not (lon_lo <= lo_lon <= lon_hi):
        raise ValueError(f"min_lon must be between {lon_lo} and {lon_hi}, got {lo_lon}")
    if not (lon_lo <= hi_lon <= lon_hi):
        raise ValueError(f"max_lon must be between {lon_lo} and {lon_hi}, got {hi_lon}")

    if not (lo_lat < hi_lat):
        raise ValueError(f"min_lat must be less than max_lat (got min_lat={lo_lat}, max_lat={hi_lat})")
    if not (lo_lon < hi_lon):
        raise ValueError(f"min_lon must be less than max_lon (got min_lon={lo_lon}, max_lon={hi_lon})")

    return clean_name, lo_lat, hi_lat, lo_lon, hi_lon


# --------------------------------------------------------------------------- #
# Public CRUD
# --------------------------------------------------------------------------- #

def add_region(name: str, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> dict:
    """Create and persist one named rectangular region.

    Validates: name non-empty, min_lat < max_lat, min_lon < max_lon, all
    four values within real coordinate ranges (-90..90 lat, -180..180 lon).
    Raises ValueError with an honest, specific message on invalid input —
    never silently clamps or guesses a corrected value.
    """
    clean_name, lo_lat, hi_lat, lo_lon, hi_lon = _validate_region(
        name, min_lat, max_lat, min_lon, max_lon
    )
    path = _regions_file()
    with _REGIONS_LOCK:
        regions = _load_regions_for_update(path)
        region = {
            "id": _next_region_id(regions),
            "name": clean_name[:200],
            "min_lat": lo_lat,
            "max_lat": hi_lat,
            "min_lon": lo_lon,
            "max_lon": hi_lon,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        regions.append(region)
        _save_regions(path, regions)
    return region


def list_regions() -> list[dict]:
    """All persisted regions, oldest first. Empty list if none. Never raises."""
    regions = _load_regions(_regions_file())
    regions.sort(key=lambda r: r.get("created_at", ""))
    return regions


def delete_region(region_id: str) -> bool:
    """Remove one region by id. Returns whether it existed and was removed.

    Raises RuntimeError (and changes nothing) when the region file cannot be read
    or is damaged, rather than writing an empty list over it.
    """
    path = _regions_file()
    with _REGIONS_LOCK:
        regions = _load_regions_for_update(path)
        kept = [r for r in regions if r.get("id") != region_id]
        if len(kept) == len(regions):
            return False
        _save_regions(path, kept)
    return True


# --------------------------------------------------------------------------- #
# Hit testing — pure point-in-rectangle over a world_pulse_geo() snapshot
# --------------------------------------------------------------------------- #

def _real_coord(item: Any) -> tuple[float, float] | None:
    """Pull a genuine (lat, lon) out of one layer item, or None.

    Defensive even though ``world_pulse_geo()`` already validates its own
    ``layers`` entries: this function's whole contract is "never fabricate
    and never raise", so it re-checks rather than trusting the caller's
    shape.
    """
    if not isinstance(item, dict):
        return None
    if "lat" not in item or "lon" not in item:
        return None
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (TypeError, ValueError):
        return None
    lat_lo, lat_hi = _LAT_RANGE
    lon_lo, lon_hi = _LON_RANGE
    if not (lat_lo <= lat <= lat_hi) or not (lon_lo <= lon <= lon_hi):
        return None
    return lat, lon


def _located_items(geo: Any) -> list[tuple[str, dict[str, Any], float, float]]:
    """Flatten every real located item out of a geo dict: (chan, item, lat, lon).

    Any shape mismatch — ``geo`` not a dict, ``layers`` missing or not a
    dict, a channel's value not a list — is treated as "no located items
    here", not an error.
    """
    out: list[tuple[str, dict[str, Any], float, float]] = []
    if not isinstance(geo, dict):
        return out
    layers = geo.get("layers")
    if not isinstance(layers, dict):
        return out
    for chan, lst in layers.items():
        if not isinstance(lst, list):
            continue
        for item in lst:
            coord = _real_coord(item)
            if coord is None:
                continue
            out.append((str(chan), item, coord[0], coord[1]))
    return out


def check_region_hits(geo: dict) -> dict[str, list[dict]]:
    """For every persisted region, the real located items that fall inside it.

    Given a ``world_pulse_geo()``-shaped dict, for every persisted region
    return the list of REAL located items (from any layer, each item
    annotated with which channel it came from via a ``chan`` key on the
    returned copy) whose lat/lon point falls inside that region's box
    (inclusive bounds). A region with zero hits is still present in the
    result with an empty list, so a caller can tell "checked, nothing
    there" from "region does not exist". Never fabricates a hit for an
    item with no real lat/lon, and never raises — a malformed geo dict is
    treated as carrying no located items, not an error.
    """
    try:
        regions = list_regions()
        located = _located_items(geo)
        result: dict[str, list[dict]] = {r.get("id", ""): [] for r in regions}
        for region in regions:
            rid = region.get("id", "")
            min_lat, max_lat = region.get("min_lat"), region.get("max_lat")
            min_lon, max_lon = region.get("min_lon"), region.get("max_lon")
            if not all(isinstance(v, (int, float)) for v in (min_lat, max_lat, min_lon, max_lon)):
                continue
            for chan, item, lat, lon in located:
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    hit = dict(item)
                    hit["chan"] = chan
                    result[rid].append(hit)
        return result
    except Exception:  # noqa: BLE001 - this function's whole contract is "never raises"
        return {}
