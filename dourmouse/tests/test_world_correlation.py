"""Hermetic tests for ``world_correlation`` — cross-channel proximity pairs.

Pure functions, no I/O: everything here runs on plain ``dict``/``float``
input and stdlib ``math``. No network, no env vars needed except where a
test is explicitly exercising the ``DOURMOUSE_CORRELATION_KM`` override
(via ``monkeypatch.setenv``, per the house convention in
``test_world_pulse.py``). The overriding contract under test: only
cross-channel pairs within threshold are ever reported, same-channel pairs
are never compared, the result is capped and closest-first, and malformed
input never raises.
"""

from __future__ import annotations

from dourmouse import world_correlation as wc


def _geo(layers: dict) -> dict:
    """Build a minimal world_pulse_geo()-shaped dict around given layers."""
    return {
        "generated_at": "2026-08-23T00:00:00+00:00",
        "pulse_score": 50,
        "pulse_label": "STABLE",
        "layers": layers,
        "counts": {k: len(v) for k, v in layers.items()},
        "unmappable": {},
    }


def _item(title: str, lat: float, lon: float, severity: str = "info") -> dict:
    return {"title": title, "summary": "", "link": "", "at": "", "severity": severity, "lat": lat, "lon": lon}


class TestHaversine:
    """Sanity-check the great-circle math against a known real distance."""

    def test_known_real_world_distance_london_paris(self):
        """London (51.5074, -0.1278) to Paris (48.8566, 2.3522).

        The commonly cited straight-line distance between these two city
        centers is ~344 km. Our own computed value must land close to
        that (a few km tolerance for which exact city-center point each
        source used) — this pins the formula/radius constant against a
        real-world number instead of only checking internal consistency.
        """
        dist = wc.haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
        assert abs(dist - 344.0) < 5.0

    def test_same_point_is_zero(self):
        assert wc.haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0

    def test_symmetric(self):
        a = wc.haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
        b = wc.haversine_km(48.8566, 2.3522, 51.5074, -0.1278)
        assert abs(a - b) < 1e-9

    def test_antipodal_points_do_not_raise(self):
        """Near-antipodal points can push the haversine `a` term a hair
        above 1.0 due to float error — must clamp, not raise from asin."""
        dist = wc.haversine_km(0.0, 0.0, 0.0, 180.0)
        assert abs(dist - (wc._EARTH_RADIUS_KM * 3.141592653589793)) < 1.0


class TestFindCorrelationsCrossChannel:
    def test_different_channels_within_threshold_correlate(self):
        """Two items from different channels, close together, must show
        up as a correlation — this is the core feature."""
        geo = _geo({
            "quakes": [_item("M4.0 near Tokyo Bay", 35.6, 139.7)],
            "flights": [_item("JAL123 Tokyo", 35.7, 139.8)],
        })
        pairs = wc.find_correlations(geo, threshold_km=50.0)
        assert len(pairs) == 1
        pair = pairs[0]
        assert {pair["a"]["chan"], pair["b"]["chan"]} == {"quakes", "flights"}
        assert pair["distance_km"] < 50.0

    def test_beyond_threshold_does_not_correlate(self):
        """Two real, located items in different channels but far apart
        must NOT be reported — proximity is the whole point."""
        geo = _geo({
            "quakes": [_item("Tokyo quake", 35.6, 139.7)],
            "flights": [_item("Flight over Paris", 48.85, 2.35)],
        })
        pairs = wc.find_correlations(geo, threshold_km=150.0)
        assert pairs == []


class TestSameChannelExcluded:
    """The important behavior: same-channel proximity is NOT a correlation.

    Two earthquakes near each other is ordinary aftershock clustering,
    not a cross-signal correlation, and reporting it would be misleading
    noise per the module's own stated rule.
    """

    def test_same_channel_pair_within_threshold_is_excluded(self):
        geo = _geo({
            "quakes": [
                _item("M4.0 quake A", 35.60, 139.70),
                _item("M3.5 quake B (aftershock)", 35.61, 139.71),
            ],
        })
        pairs = wc.find_correlations(geo, threshold_km=100.0)
        assert pairs == []

    def test_same_channel_excluded_even_when_a_cross_channel_pair_also_exists(self):
        """Mixed input: a same-channel close pair must be dropped while a
        genuine cross-channel close pair in the same data still surfaces."""
        geo = _geo({
            "quakes": [
                _item("M4.0 quake A", 35.60, 139.70),
                _item("M3.5 quake B (aftershock)", 35.61, 139.71),
            ],
            "disasters": [_item("Flood alert near Tokyo", 35.62, 139.72, severity="high")],
        })
        pairs = wc.find_correlations(geo, threshold_km=100.0)
        chans_seen = [tuple(sorted((p["a"]["chan"], p["b"]["chan"]))) for p in pairs]
        assert ("quakes", "quakes") not in chans_seen
        assert all("disasters" in c for c in chans_seen)
        assert len(pairs) == 2  # disasters<->quakeA and disasters<->quakeB


class TestThresholdEnvOverride:
    def test_env_var_sets_default_threshold(self, monkeypatch):
        """With no explicit threshold_km, DOURMOUSE_CORRELATION_KM governs."""
        monkeypatch.setenv("DOURMOUSE_CORRELATION_KM", "10")
        geo = _geo({
            "quakes": [_item("quake", 35.60, 139.70)],
            "flights": [_item("flight", 35.62, 139.72)],  # a few km away
        })
        # ~2-3 km apart: within a 10 km env threshold...
        pairs = wc.find_correlations(geo)
        assert len(pairs) == 1

    def test_env_var_can_exclude_when_tight(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_CORRELATION_KM", "0.01")
        geo = _geo({
            "quakes": [_item("quake", 35.60, 139.70)],
            "flights": [_item("flight", 35.62, 139.72)],
        })
        pairs = wc.find_correlations(geo)
        assert pairs == []

    def test_explicit_argument_overrides_env_var(self, monkeypatch):
        """An explicit threshold_km must win over the env var."""
        monkeypatch.setenv("DOURMOUSE_CORRELATION_KM", "0.01")
        geo = _geo({
            "quakes": [_item("quake", 35.60, 139.70)],
            "flights": [_item("flight", 35.62, 139.72)],
        })
        pairs = wc.find_correlations(geo, threshold_km=50.0)
        assert len(pairs) == 1

    def test_default_threshold_is_150km_when_unset(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_CORRELATION_KM", raising=False)
        assert wc._threshold_km(None) == 150.0


class TestCapAndOrdering:
    def test_results_are_closest_first(self):
        geo = _geo({
            "quakes": [_item("near quake", 35.60, 139.70)],
            "flights": [
                _item("far flight", 36.00, 140.00),
                _item("close flight", 35.601, 139.701),
            ],
        })
        pairs = wc.find_correlations(geo, threshold_km=200.0)
        assert len(pairs) == 2
        assert pairs[0]["distance_km"] <= pairs[1]["distance_km"]
        assert pairs[0]["b"]["title"] == "close flight"

    def test_capped_to_20_pairs_with_more_available(self):
        """25 quakes clustered together plus 1 flight at the same point
        yields 25 real cross-channel pairs, all at distance ~0 — the
        result must be capped to the closest 20, not all 25."""
        quakes = [_item(f"quake {i}", 35.0 + i * 0.0001, 139.0) for i in range(25)]
        flights = [_item("flight", 35.0, 139.0)]
        geo = _geo({"quakes": quakes, "flights": flights})
        pairs = wc.find_correlations(geo, threshold_km=500.0)
        assert len(pairs) == 20

    def test_cap_keeps_the_truly_closest_pairs(self):
        """When more than 20 pairs qualify, the ones kept must be the 20
        smallest distances, not an arbitrary subset."""
        quakes = [_item(f"quake {i}", 35.0 + i * 0.01, 139.0) for i in range(25)]
        flights = [_item("flight", 35.0, 139.0)]
        geo = _geo({"quakes": quakes, "flights": flights})
        pairs = wc.find_correlations(geo, threshold_km=500.0)
        assert len(pairs) == 20
        kept_titles = {p["a"]["title"] for p in pairs} | {p["b"]["title"] for p in pairs}
        # the 20 closest quakes are indices 0..19 (ascending lat offset == ascending distance)
        expected = {f"quake {i}" for i in range(20)} | {"flight"}
        assert kept_titles == expected


class TestMalformedInputNeverRaises:
    """The house rule: malformed/empty upstream data degrades to `[]`,
    never to an exception — a correlation view must not take the whole
    monitor down because of one bad payload."""

    def test_empty_dict(self):
        assert wc.find_correlations({}) == []

    def test_missing_layers_key(self):
        assert wc.find_correlations({"generated_at": "now"}) == []

    def test_layers_not_a_dict(self):
        assert wc.find_correlations({"layers": "nope"}) == []

    def test_layer_not_a_list(self):
        assert wc.find_correlations({"layers": {"quakes": "nope"}}) == []

    def test_item_not_a_dict(self):
        assert wc.find_correlations({"layers": {"quakes": ["nope"], "flights": [_item("f", 1, 1)]}}) == []

    def test_item_missing_lat_lon(self):
        geo = {"layers": {
            "quakes": [{"title": "no coords", "severity": "info"}],
            "flights": [_item("flight", 1.0, 1.0)],
        }}
        assert wc.find_correlations(geo) == []

    def test_item_with_non_numeric_lat_lon(self):
        geo = {"layers": {
            "quakes": [{"title": "bad coords", "lat": "north", "lon": "east", "severity": "info"}],
            "flights": [_item("flight", 1.0, 1.0)],
        }}
        assert wc.find_correlations(geo) == []

    def test_none_input(self):
        assert wc.find_correlations(None) == []

    def test_single_channel_only_no_error(self):
        geo = _geo({"quakes": [_item("only one channel", 10.0, 10.0)]})
        assert wc.find_correlations(geo) == []
