"""Hermetic tests for World Watch Regions (v1.0).

No network, no real filesystem: every test points ``DOURMOUSE_WORKSPACE`` /
``DOURMOUSE_WATCH_REGIONS_FILE`` at ``tmp_path`` via ``monkeypatch.setenv``,
mirroring ``test_live_feeds.TestTasks``'s hermeticity convention for the
tasks file. The overriding contract under test: a hit is only ever a real
item with real coordinates genuinely inside a region's box, CRUD never
raises on "not found", and ``check_region_hits`` never raises even on a
malformed snapshot.
"""

from __future__ import annotations

import pytest

from dourmouse import world_watch_regions as wr


@pytest.fixture(autouse=True)
def _regions_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_WATCH_REGIONS_FILE", str(tmp_path / "watch_regions.json"))


def _geo(layers: dict) -> dict:
    """Build a minimal world_pulse_geo()-shaped dict for a given layers map."""
    return {
        "generated_at": "2026-08-23T00:00:00+00:00",
        "pulse_score": 62,
        "pulse_label": "STABLE",
        "layers": layers,
        "counts": {k: len(v) for k, v in layers.items()},
        "unmappable": {},
    }


class TestCrud:
    def test_add_region_returns_created_record(self):
        region = wr.add_region("Bay Area", 36.0, 39.0, -123.0, -121.0)
        assert region["id"] == "region-1"
        assert region["name"] == "Bay Area"
        assert region["min_lat"] == 36.0
        assert region["max_lat"] == 39.0
        assert region["min_lon"] == -123.0
        assert region["max_lon"] == -121.0
        assert "created_at" in region

    def test_ids_increment_and_list_is_oldest_first(self):
        first = wr.add_region("A", 0.0, 1.0, 0.0, 1.0)
        second = wr.add_region("B", 10.0, 11.0, 10.0, 11.0)
        listed = wr.list_regions()
        assert [r["id"] for r in listed] == [first["id"], second["id"]]
        assert [r["id"] for r in listed] == ["region-1", "region-2"]

    def test_list_regions_empty_when_none_created(self):
        assert wr.list_regions() == []

    def test_regions_persist_across_calls(self):
        wr.add_region("Persisted", 0.0, 1.0, 0.0, 1.0)
        # Fresh read from disk (module keeps no in-memory state) must see it.
        assert any(r["name"] == "Persisted" for r in wr.list_regions())

    def test_delete_existing_region(self):
        region = wr.add_region("Doomed", 0.0, 1.0, 0.0, 1.0)
        assert wr.delete_region(region["id"]) is True
        assert wr.list_regions() == []

    def test_delete_nonexistent_region_returns_false_not_error(self):
        """The house convention (see live_feeds.complete_task): 'not found'
        is a normal False return, never an exception."""
        assert wr.delete_region("region-does-not-exist") is False


class TestValidation:
    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="non-empty name"):
            wr.add_region("   ", 0.0, 1.0, 0.0, 1.0)

    def test_min_lat_equal_max_lat_rejected(self):
        with pytest.raises(ValueError, match="min_lat must be less than max_lat"):
            wr.add_region("flat", 10.0, 10.0, 0.0, 1.0)

    def test_min_lat_greater_than_max_lat_rejected(self):
        with pytest.raises(ValueError, match="min_lat must be less than max_lat"):
            wr.add_region("inverted", 10.0, 5.0, 0.0, 1.0)

    def test_min_lon_greater_equal_max_lon_rejected(self):
        with pytest.raises(ValueError, match="min_lon must be less than max_lon"):
            wr.add_region("inverted", 0.0, 1.0, 50.0, 50.0)

    def test_lat_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="min_lat must be between"):
            wr.add_region("bad", -91.0, 0.0, 0.0, 1.0)
        with pytest.raises(ValueError, match="max_lat must be between"):
            wr.add_region("bad", 0.0, 90.1, 0.0, 1.0)

    def test_lon_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="min_lon must be between"):
            wr.add_region("bad", 0.0, 1.0, -180.1, 0.0)
        with pytest.raises(ValueError, match="max_lon must be between"):
            wr.add_region("bad", 0.0, 1.0, 0.0, 180.1)

    def test_never_clamps_a_bad_value(self):
        """Rule: never silently coerce a corrected value — an out-of-range
        box must be rejected outright, not clamped to the nearest legal
        rectangle."""
        with pytest.raises(ValueError):
            wr.add_region("bad", -95.0, 95.0, -185.0, 185.0)
        assert wr.list_regions() == []

    def test_non_numeric_coordinate_rejected(self):
        with pytest.raises(ValueError, match="min_lat must be a real number"):
            wr.add_region("bad", "not-a-number", 1.0, 0.0, 1.0)


class TestCheckRegionHits:
    def test_item_inside_box_is_a_hit(self):
        region = wr.add_region("Japan box", 30.0, 40.0, 135.0, 145.0)
        geo = _geo({"quakes": [{"title": "M5 Honshu", "lat": 35.0, "lon": 140.0}]})
        hits = wr.check_region_hits(geo)
        assert len(hits[region["id"]]) == 1
        assert hits[region["id"]][0]["title"] == "M5 Honshu"
        assert hits[region["id"]][0]["chan"] == "quakes"

    def test_item_outside_box_is_excluded(self):
        region = wr.add_region("Japan box", 30.0, 40.0, 135.0, 145.0)
        geo = _geo({"quakes": [{"title": "M5 Nowhere near", "lat": -10.0, "lon": 20.0}]})
        hits = wr.check_region_hits(geo)
        assert hits[region["id"]] == []

    def test_boundary_point_is_inclusive(self):
        """The spec is explicit: bounds are inclusive, so a point sitting
        exactly on an edge/corner of the box must still count."""
        region = wr.add_region("Exact box", 10.0, 20.0, 100.0, 110.0)
        geo = _geo({"disasters": [{"title": "on the corner", "lat": 10.0, "lon": 100.0}]})
        hits = wr.check_region_hits(geo)
        assert len(hits[region["id"]]) == 1

    def test_zero_hits_region_still_present_in_result(self):
        """A region that matched nothing must still appear (with an empty
        list) so a caller can tell 'checked, nothing there' apart from
        'this region id does not exist'."""
        region = wr.add_region("Empty box", 0.0, 1.0, 0.0, 1.0)
        hits = wr.check_region_hits(_geo({}))
        assert region["id"] in hits
        assert hits[region["id"]] == []

    def test_unknown_region_id_not_in_result(self):
        wr.add_region("Some box", 0.0, 1.0, 0.0, 1.0)
        hits = wr.check_region_hits(_geo({}))
        assert "region-does-not-exist" not in hits

    def test_item_without_coordinates_never_fabricates_a_hit(self):
        region = wr.add_region("Whole world", -90.0, 90.0, -180.0, 180.0)
        geo = _geo({"cyber": [{"title": "unlocated advisory, no lat/lon"}]})
        hits = wr.check_region_hits(geo)
        assert hits[region["id"]] == []

    def test_hits_span_multiple_layers(self):
        region = wr.add_region("Whole world", -90.0, 90.0, -180.0, 180.0)
        geo = _geo(
            {
                "quakes": [{"title": "a quake", "lat": 1.0, "lon": 1.0}],
                "flights": [{"title": "a flight", "lat": -1.0, "lon": -1.0}],
            }
        )
        hits = wr.check_region_hits(geo)
        chans = {h["chan"] for h in hits[region["id"]]}
        assert chans == {"quakes", "flights"}

    def test_malformed_geo_dict_does_not_raise(self):
        region = wr.add_region("Whole world", -90.0, 90.0, -180.0, 180.0)
        for bad in ({}, {"layers": "not a dict"}, {"layers": {"quakes": "not a list"}}, None, "garbage"):
            hits = wr.check_region_hits(bad)  # must not raise
            if isinstance(bad, dict):
                assert hits.get(region["id"], []) == []

    def test_empty_geo_dict_yields_no_hits_for_any_region(self):
        region = wr.add_region("Whole world", -90.0, 90.0, -180.0, 180.0)
        hits = wr.check_region_hits({})
        assert hits[region["id"]] == []

    def test_annotated_copy_does_not_mutate_the_source_item(self):
        wr.add_region("Whole world", -90.0, 90.0, -180.0, 180.0)
        source_item = {"title": "a quake", "lat": 1.0, "lon": 1.0}
        geo = _geo({"quakes": [source_item]})
        wr.check_region_hits(geo)
        assert "chan" not in source_item
