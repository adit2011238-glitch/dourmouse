"""World Correlation — cross-channel proximity detection for World Pulse.

``world_pulse.world_pulse_geo()`` hands the map a set of real, located
items grouped by channel (quakes, disasters, flights, ...). On its own
that is "a list of dots": every point is real, but nothing relates one
point to another. This module is the first step past that — it looks at
every pair of located items that come from TWO DIFFERENT channels and
reports the ones that are geographically close, using the real haversine
distance between their real coordinates. Two disaster alerts of different
types 40 km apart, or an earthquake sitting under a live flight path, is a
signal worth surfacing that a flat list hides.

What this deliberately does NOT do: it never claims causation. A close
pair is reported as exactly that — a distance between two real points —
never as "X caused Y" or "X is related to Y". The reader draws whatever
conclusion the proximity warrants; we only supply the (real) number.

Same-channel pairs are excluded on purpose. Two earthquakes near each
other is ordinary aftershock clustering, not a cross-signal correlation,
and reporting it as one would manufacture insight that isn't there.

Complexity: this is deliberately a plain O(n^2) all-pairs-across-channels
scan (for each item, compare against every item in every OTHER channel).
That is fine at this scale — every World Pulse channel is capped at
``_MAX_ITEMS_PER_SOURCE`` (8) items, so even with every geo channel full
the total point count is in the dozens, meaning at most a few hundred
candidate pairs. This does NOT scale to thousands of items; if a future
channel starts returning that many located points, this module should
switch to a spatial index (e.g. a grid or k-d tree) before comparing
pairs. Written honestly rather than pretending it already scales.

Deterministic and self-contained: no LLM calls, no randomness, no network
I/O, no new pip dependency (the great-circle math is plain stdlib
``math``). Given the same ``world_pulse_geo()``-shaped input, this module
always returns the same output.
"""

from __future__ import annotations

import math
import os

#: Default proximity threshold in kilometers when neither an explicit
#: ``threshold_km`` argument nor the ``DOURMOUSE_CORRELATION_KM`` env var
#: is set.
_DEFAULT_THRESHOLD_KM = 150.0

#: Earth's mean radius in kilometers (WGS-84 authalic radius, the same
#: constant conventionally used for haversine great-circle distance).
_EARTH_RADIUS_KM = 6371.0088

#: However many geographically close pairs are real, only the closest
#: this many are worth surfacing to a reader — the rest add noise, not
#: insight.
_MAX_PAIRS = 20


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Real great-circle distance in kilometers between two lat/lon points.

    Standard haversine formula, Earth radius 6371.0088 km. Pure stdlib
    math — no external dependency needed or wanted for this.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    # Clamp for float safety: `a` can drift a hair above 1.0 for
    # antipodal-ish points, which would make asin raise.
    a = min(1.0, max(0.0, a))
    c = 2 * math.asin(math.sqrt(a))
    return _EARTH_RADIUS_KM * c


def _threshold_km(explicit: float | None) -> float:
    """Resolve the effective threshold: explicit arg > env var > default.

    Mirrors the ``_ttl()`` pattern in ``world_pulse.py`` — an unparsable
    env value falls back to the default rather than raising.
    """
    if explicit is not None:
        return explicit
    try:
        return float(os.environ.get("DOURMOUSE_CORRELATION_KM", _DEFAULT_THRESHOLD_KM))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_KM


def _extract_points(geo: dict) -> list[tuple[str, dict]]:
    """Flatten a (possibly malformed) geo dict into ``(chan, item)`` pairs.

    Defensive by design: this never raises. A missing ``layers`` key, a
    layer that isn't a list, or an item missing/mis-typed lat/lon just
    contributes no points rather than blowing up the whole call — a
    malformed upstream snapshot must not take the correlation view down
    with it.
    """
    points: list[tuple[str, dict]] = []
    if not isinstance(geo, dict):
        return points
    layers = geo.get("layers")
    if not isinstance(layers, dict):
        return points
    for chan, items in layers.items():
        if not isinstance(chan, str) or not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            lat, lon = it.get("lat"), it.get("lon")
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                continue
            points.append((chan, {
                "chan": chan,
                "title": str(it.get("title", "") or ""),
                "lat": lat_f,
                "lon": lon_f,
                "severity": str(it.get("severity", "") or ""),
            }))
    return points


def find_correlations(geo: dict, threshold_km: float | None = None) -> list[dict]:
    """Cross-channel proximity pairs from a ``world_pulse_geo()`` dict.

    Finds every pair of located items from TWO DIFFERENT channels whose
    real haversine distance is <= ``threshold_km`` (default: the
    ``DOURMOUSE_CORRELATION_KM`` env var, else 150.0 km). Same-channel
    pairs are never compared — see the module docstring for why. Returns
    a list of dicts, closest-first, capped to the closest 20 pairs:

        [{"distance_km": float,
          "a": {"chan": str, "title": str, "lat": float, "lon": float, "severity": str},
          "b": {"chan": str, "title": str, "lat": float, "lon": float, "severity": str}},
         ...]

    Never fabricates: every pair reported was actually computed from two
    real coordinates present in the input. Never raises: malformed or
    empty input (missing ``layers``, a non-list layer, an item without
    usable lat/lon) is treated as contributing no points, yielding ``[]``.
    """
    threshold = _threshold_km(threshold_km)
    points = _extract_points(geo)

    pairs: list[dict] = []
    n = len(points)
    # O(n^2) all-pairs-across-channels scan — see the module docstring
    # for why that is an acceptable tradeoff at this scale (dozens of
    # points, not thousands).
    for i in range(n):
        chan_a, item_a = points[i]
        for j in range(i + 1, n):
            chan_b, item_b = points[j]
            if chan_a == chan_b:
                continue
            dist = haversine_km(item_a["lat"], item_a["lon"], item_b["lat"], item_b["lon"])
            if dist <= threshold:
                pairs.append({
                    "distance_km": dist,
                    "a": item_a,
                    "b": item_b,
                })

    pairs.sort(key=lambda p: p["distance_km"])
    return pairs[:_MAX_PAIRS]
