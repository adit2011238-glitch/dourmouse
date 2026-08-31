"""dourmouse/gdelt_graph.py — real-time GDELT ingestion + kinetic
knowledge graph. See that module's own docstring for what's real
(GDELT GKG 2.1, live field layout confirmed against a real downloaded
file this session) vs deliberately not built (Streamparse/Storm).

Real hermetic fixture: hand-built 27-field GKG rows using the EXACT
real field values/format observed live against
``20260831190000.gkg.csv.zip`` before this module was written (Toronto/
Ontario V2Locations entry, "regina peter"/"blair berk" Persons, "los
angeles county public defender office" Organizations, a real V2Tone
comma list) — not invented data, transcribed from an actual GDELT
response.
"""

from __future__ import annotations

import time

from dourmouse import gdelt_graph as gg


def _row(
    record_id="20260831190000-0",
    persons="regina peter;blair berk",
    organizations="los angeles county public defender office",
    locations="4#Toronto, Ontario, Canada#CA#CA08#43.6667#-79.4167#-574890",
    tone="-4.77453580901857,0.795755968169761,5.57029177718833,6.36604774535809,24.6684350132626,0.795755968169761,334",
    source="tmz.com",
    url="https://www.tmz.com/2026/08/31/example/",
    date="20260831190000",
    n_fields=27,
) -> str:
    fields = [""] * n_fields
    fields[gg._COL_ID] = record_id
    fields[gg._COL_DATE] = date
    fields[gg._COL_SOURCE] = source
    fields[gg._COL_URL] = url
    fields[gg._COL_PERSONS] = persons
    fields[gg._COL_ORGANIZATIONS] = organizations
    fields[gg._COL_LOCATIONS_V2] = locations
    fields[gg._COL_TONE] = tone
    return "\t".join(fields)


class TestParseGkgRow:
    def test_real_row_parses_correctly(self):
        rec = gg.parse_gkg_row(_row())
        assert rec is not None
        assert rec.record_id == "20260831190000-0"
        assert rec.source == "tmz.com"
        assert rec.persons == ["regina peter", "blair berk"]
        assert rec.organizations == ["los angeles county public defender office"]
        assert len(rec.locations) == 1
        assert rec.locations[0]["name"] == "Toronto, Ontario, Canada"
        assert rec.locations[0]["country_code"] == "CA"
        assert rec.locations[0]["lat"] == 43.6667
        assert rec.locations[0]["lon"] == -79.4167
        assert rec.tone == -4.77453580901857

    def test_empty_line_is_none(self):
        assert gg.parse_gkg_row("") is None
        assert gg.parse_gkg_row("   \n") is None

    def test_too_few_fields_is_none_not_a_crash(self):
        assert gg.parse_gkg_row("a\tb\tc") is None

    def test_missing_record_id_is_none(self):
        assert gg.parse_gkg_row(_row(record_id="")) is None

    def test_no_persons_or_orgs_is_still_a_valid_record(self):
        rec = gg.parse_gkg_row(_row(persons="", organizations="", locations=""))
        assert rec is not None
        assert rec.persons == []
        assert rec.organizations == []
        assert rec.locations == []


class TestParseV2Locations:
    def test_multiple_real_entries(self):
        raw = (
            "4#Toronto, Ontario, Canada#CA#CA08#43.6667#-79.4167#-574890;"
            "2#Colorado, United States#US#USCO#39.0646#-105.327#CO"
        )
        out = gg._parse_v2_locations(raw)
        assert len(out) == 2
        assert out[0]["name"] == "Toronto, Ontario, Canada"
        assert out[1]["name"] == "Colorado, United States"

    def test_malformed_entry_skipped_not_crashed(self):
        raw = "not-enough-hash-parts;4#Real Place#US#US01#1.0#2.0#123"
        out = gg._parse_v2_locations(raw)
        assert len(out) == 1
        assert out[0]["name"] == "Real Place"

    def test_empty_string_gives_empty_list(self):
        assert gg._parse_v2_locations("") == []


class TestParseTone:
    def test_real_tone_value(self):
        assert gg._parse_tone("-4.77,0.79,5.57") == -4.77

    def test_empty_is_none(self):
        assert gg._parse_tone("") is None

    def test_garbage_is_none_not_a_crash(self):
        assert gg._parse_tone("not-a-number,1,2") is None


class TestParseGkgText:
    def test_multiple_rows_and_bad_row_skipped(self):
        text = "\n".join([_row(record_id="a-0"), "bad\trow", _row(record_id="a-1")])
        recs = gg.parse_gkg_text(text)
        assert [r.record_id for r in recs] == ["a-0", "a-1"]

    def test_max_records_caps_output(self):
        text = "\n".join(_row(record_id=f"a-{i}") for i in range(10))
        recs = gg.parse_gkg_text(text, max_records=3)
        assert len(recs) == 3


class TestKineticGraphIngest:
    def test_creates_nodes_and_edges_from_one_record(self):
        graph = gg.KineticGraph()
        rec = gg.parse_gkg_row(_row())
        graph.ingest_record(rec)
        nodes, edges = graph.counts()
        # 2 persons + 1 org + 1 location = 4 entities
        assert nodes == 4
        # C(4,2) = 6 co-occurrence pairs
        assert edges == 6

    def test_repeated_mention_increments_weight_not_duplicate_node(self):
        graph = gg.KineticGraph()
        rec = gg.parse_gkg_row(_row())
        graph.ingest_record(rec)
        graph.ingest_record(rec)
        nodes, edges = graph.counts()
        assert nodes == 4
        assert edges == 6
        snap = graph.snapshot()
        person_node = next(n for n in snap["nodes"] if n["label"] == "Regina Peter")
        assert person_node["weight"] == 2
        edge = next(e for e in snap["edges"])
        assert edge["weight"] >= 1

    def test_empty_record_ingests_nothing(self):
        graph = gg.KineticGraph()
        rec = gg.parse_gkg_row(_row(persons="", organizations="", locations=""))
        graph.ingest_record(rec)
        assert graph.counts() == (0, 0)

    def test_entity_cap_truncates_huge_records(self):
        graph = gg.KineticGraph()
        many_locations = ";".join(
            f"1#Place{i}#US#US0{i}#{i}.0#{i}.0#{i}" for i in range(20)
        )
        rec = gg.parse_gkg_row(_row(persons="", organizations="", locations=many_locations))
        assert len(rec.locations) == 20
        graph.ingest_record(rec)
        nodes, _ = graph.counts()
        assert nodes == gg._MAX_ENTITIES_PER_RECORD


class TestKineticGraphPrune:
    def test_prune_drops_stale_nodes_and_edges(self):
        graph = gg.KineticGraph()
        rec = gg.parse_gkg_row(_row())
        old_ts = time.time() - 10_000
        graph.ingest_record(rec, now=old_ts)
        dropped = graph.prune(max_age_seconds=100, now=time.time())
        assert dropped["nodes_dropped"] == 4
        assert dropped["edges_dropped"] == 6
        assert graph.counts() == (0, 0)

    def test_prune_keeps_fresh_entries(self):
        graph = gg.KineticGraph()
        rec = gg.parse_gkg_row(_row())
        graph.ingest_record(rec, now=time.time())
        dropped = graph.prune(max_age_seconds=3600, now=time.time())
        assert dropped == {"nodes_dropped": 0, "edges_dropped": 0}
        assert graph.counts() == (4, 6)

    def test_mixed_age_prunes_only_stale_ones(self):
        graph = gg.KineticGraph()
        old_rec = gg.parse_gkg_row(_row(record_id="old", persons="old person", organizations="", locations=""))
        new_rec = gg.parse_gkg_row(_row(record_id="new", persons="new person", organizations="", locations=""))
        now = time.time()
        graph.ingest_record(old_rec, now=now - 10_000)
        graph.ingest_record(new_rec, now=now)
        graph.prune(max_age_seconds=100, now=now)
        nodes, _ = graph.counts()
        assert nodes == 1
        snap = graph.snapshot()
        assert snap["nodes"][0]["label"] == "New Person"


class TestKineticGraphSnapshot:
    def test_snapshot_caps_to_limit_and_filters_edges(self):
        graph = gg.KineticGraph()
        for i in range(5):
            rec = gg.parse_gkg_row(
                _row(record_id=f"r{i}", persons=f"person {i}", organizations="", locations="")
            )
            graph.ingest_record(rec)
        snap = graph.snapshot(limit_nodes=2)
        assert len(snap["nodes"]) == 2
        assert snap["total_nodes"] == 5
        # No edges — each record's lone person entity never co-occurred with another.
        assert snap["edges"] == []

    def test_snapshot_orders_by_weight_descending(self):
        graph = gg.KineticGraph()
        rec_a = gg.parse_gkg_row(_row(persons="popular person", organizations="", locations=""))
        rec_b = gg.parse_gkg_row(_row(record_id="b", persons="rare person", organizations="", locations=""))
        graph.ingest_record(rec_a)
        graph.ingest_record(rec_a)
        graph.ingest_record(rec_a)
        graph.ingest_record(rec_b)
        snap = graph.snapshot()
        assert snap["nodes"][0]["label"] == "Popular Person"
        assert snap["nodes"][0]["weight"] == 3


class TestFetchLatestGkgUrl:
    def test_real_manifest_format_parses_correctly(self, monkeypatch):
        manifest = (
            "80936 abcd http://data.gdeltproject.org/gdeltv2/20260831190000.export.CSV.zip\n"
            "132169 abcd http://data.gdeltproject.org/gdeltv2/20260831190000.mentions.CSV.zip\n"
            "6691084 abcd http://data.gdeltproject.org/gdeltv2/20260831190000.gkg.csv.zip\n"
        )

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return manifest.encode()

        monkeypatch.setattr(gg.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
        url = gg.fetch_latest_gkg_url()
        assert url == "http://data.gdeltproject.org/gdeltv2/20260831190000.gkg.csv.zip"

    def test_unreachable_host_returns_none_not_a_crash(self, monkeypatch):
        def _raise(*a, **k):
            raise gg.urllib.error.URLError("no route to host")

        monkeypatch.setattr(gg.urllib.request, "urlopen", _raise)
        assert gg.fetch_latest_gkg_url() is None

    def test_malformed_manifest_returns_none(self, monkeypatch):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"nonsense, no gkg line here"

        monkeypatch.setattr(gg.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
        assert gg.fetch_latest_gkg_url() is None


class TestFetchGkgRecords:
    def test_real_zip_round_trip(self, monkeypatch):
        import io
        import zipfile

        text = "\n".join([_row(record_id="z-0"), _row(record_id="z-1")])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("20260831190000.gkg.csv", text)
        zip_bytes = buf.getvalue()

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return zip_bytes

        monkeypatch.setattr(gg.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
        recs = gg.fetch_gkg_records("http://example.com/fake.gkg.csv.zip")
        assert [r.record_id for r in recs] == ["z-0", "z-1"]

    def test_corrupt_zip_returns_empty_list(self, monkeypatch):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"this is not a real zip file"

        monkeypatch.setattr(gg.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
        assert gg.fetch_gkg_records("http://example.com/fake.gkg.csv.zip") == []

    def test_network_failure_returns_empty_list(self, monkeypatch):
        def _raise(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(gg.urllib.request, "urlopen", _raise)
        assert gg.fetch_gkg_records("http://example.com/fake.gkg.csv.zip") == []


class TestPollOnce:
    def _reset_module_state(self, monkeypatch):
        monkeypatch.setattr(gg, "_last_processed_url", None)
        monkeypatch.setattr(gg, "_last_fetch_at", None)
        monkeypatch.setattr(gg, "_last_error", None)
        fresh_graph = gg.KineticGraph()
        monkeypatch.setattr(gg, "_GRAPH", fresh_graph)

    def test_first_poll_fetches_and_ingests(self, monkeypatch):
        self._reset_module_state(monkeypatch)
        monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: "http://example.com/a.gkg.csv.zip")
        monkeypatch.setattr(gg, "fetch_gkg_records", lambda url, **k: [gg.parse_gkg_row(_row())])
        result = gg.poll_once()
        assert result["ok"] is True
        assert result["fetched"] is True
        assert result["records"] == 1
        nodes, _ = gg.get_graph().counts()
        assert nodes == 4

    def test_same_url_twice_skips_the_second_fetch(self, monkeypatch):
        self._reset_module_state(monkeypatch)
        monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: "http://example.com/a.gkg.csv.zip")
        calls = []

        def _fake_fetch(url, **k):
            calls.append(url)
            return [gg.parse_gkg_row(_row())]

        monkeypatch.setattr(gg, "fetch_gkg_records", _fake_fetch)
        gg.poll_once()
        result2 = gg.poll_once()
        assert result2["fetched"] is False
        assert len(calls) == 1

    def test_unreachable_gdelt_is_honest_not_a_crash(self, monkeypatch):
        self._reset_module_state(monkeypatch)
        monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: None)
        result = gg.poll_once()
        assert result["ok"] is False
        assert "GDELT" in result["error"]


class TestGraphStatus:
    def test_reflects_real_graph_counts(self, monkeypatch):
        fresh_graph = gg.KineticGraph()
        monkeypatch.setattr(gg, "_GRAPH", fresh_graph)
        rec = gg.parse_gkg_row(_row())
        fresh_graph.ingest_record(rec)
        status = gg.graph_status()
        assert status["node_count"] == 4
        assert status["edge_count"] == 6

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GDELT_POLLER", "0")
        assert gg.graph_status()["enabled"] is False


class TestPollerLifecycle:
    def test_disabled_by_default_in_tests(self, monkeypatch):
        # The _gdelt_poller_off autouse fixture in conftest.py already
        # sets this — asserted here explicitly so a future conftest
        # regression fails loudly and specifically, not via a mysterious
        # real network call somewhere else in the suite.
        assert gg.start_gdelt_graph_poller() is False

    def test_enabled_starts_a_real_idempotent_thread(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GDELT_POLLER", "1")
        monkeypatch.setenv("DOURMOUSE_GDELT_POLL_INTERVAL", "9999")
        monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: None)
        try:
            assert gg.start_gdelt_graph_poller() is True
            assert gg.start_gdelt_graph_poller() is True  # idempotent
        finally:
            gg.stop_gdelt_graph_poller()

    def test_stop_actually_stops_the_thread(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GDELT_POLLER", "1")
        monkeypatch.setenv("DOURMOUSE_GDELT_POLL_INTERVAL", "9999")
        monkeypatch.setattr(gg, "fetch_latest_gkg_url", lambda: None)
        gg.start_gdelt_graph_poller()
        thread = gg._poller_thread
        gg.stop_gdelt_graph_poller()
        assert thread is not None
        assert not thread.is_alive()
