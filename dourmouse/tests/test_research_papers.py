"""PL-10 T1 step 1: paper search over OpenAlex, Semantic Scholar and arXiv.

The OpenAlex and arXiv fixtures are trimmed from real responses captured on
2026-11-13 (a search for "spacing effect learning"). Keyless Semantic Scholar
answered 429 that day, so its fixture is a constructed sample in the shape the
Graph API documents; it tests parsing, not a real record.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from dourmouse.research_pipeline import papers as P

OPENALEX_WORK = {
    "id": "https://openalex.org/W2167585898",
    "doi": "https://doi.org/10.1101/lm.488707",
    "display_name": "Neurogenesis and the spacing effect: Learning over time enhances memory and the survival of new neurons",
    "publication_year": 2007,
    "publication_date": "2007-05-01",
    "type": "preprint",
    "cited_by_count": 194,
    "authorships": [
        {"author_position": "first", "author": {"display_name": "Helene M. Sisti"}},
        {"author_position": "last", "author": {"display_name": "Tracey J. Shors"}},
    ],
    "primary_location": {
        "landing_page_url": "https://doi.org/10.1101/lm.488707",
        "pdf_url": "http://learnmem.cshlp.org/content/14/5/368.full.pdf",
        "source": {"display_name": "Learning & Memory", "type": "journal",
                   "host_organization_name": "Cold Spring Harbor Laboratory Press"},
        "version": "publishedVersion",
    },
    "best_oa_location": {"pdf_url": "http://learnmem.cshlp.org/content/14/5/368.full.pdf"},
    "abstract_inverted_index": {
        "Information": [0], "that": [1], "is": [2, 6], "spaced": [3], "over": [4], "time": [5],
        "better": [7], "remembered": [8],
    },
    "biblio": {"volume": "14", "issue": "5", "first_page": "368", "last_page": "375"},
    "ids": {"openalex": "https://openalex.org/W2167585898", "doi": "https://doi.org/10.1101/lm.488707"},
}

ARXIV_FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/PHaOdwbThimhucFL8OondkvMk18</id>
  <entry>
    <id>http://arxiv.org/abs/2502.06192v2</id>
    <title>Right Time to Learn:Promoting Generalization via Bio-inspired Spacing Effect in Knowledge Distillation</title>
    <published>2025-02-10T06:31:01Z</published>
    <summary>Knowledge distillation (KD) is a powerful strategy for training deep neural networks (DNNs).</summary>
    <author><name>Guanglong Sun</name></author>
    <author><name>Hongwei Yan</name></author>
    <link href="https://arxiv.org/pdf/2502.06192v2" rel="related" type="application/pdf" title="pdf"/>
  </entry>
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_x</id>
    <title>Error</title>
    <summary>incorrect id format for x</summary>
  </entry>
</feed>
"""

# Constructed sample (see the module docstring), Graph API field names.
S2_PAPER = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "externalIds": {"DOI": "10.5555/3295222.3295349", "ArXiv": "1706.03762"},
    "title": "Attention is All you Need",
    "abstract": "The dominant sequence transduction models are based on complex recurrent networks.",
    "venue": "Neural Information Processing Systems",
    "year": 2017,
    "citationCount": 100000,
    "openAccessPdf": {"url": "https://example.org/attention.pdf"},
    "publicationTypes": ["JournalArticle", "Conference"],
    "journal": {"name": "Advances in Neural Information Processing Systems", "volume": "30", "pages": " 5998-6008"},
    "publicationDate": "2017-06-12",
    "url": "https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam M. Shazeer"}],
}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeThrottle:
    def __init__(self):
        self.waits = 0

    def wait(self):
        self.waits += 1


def _client(tmp_path, responses, **kw):
    transport = FakeTransport(responses)
    sleeps: list[float] = []
    client = P.PaperClient(
        transport=transport, cache_dir=tmp_path / "cache", sleep=sleeps.append,
        throttles={s: FakeThrottle() for s in P.MIN_INTERVAL}, openalex_key=kw.pop("openalex_key", ""),
        s2_key=kw.pop("s2_key", ""), **kw,
    )
    return client, transport, sleeps


def _ok(obj) -> tuple[int, dict, bytes]:
    body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return 200, {}, body


# ---------------------------------------------------------------- normalizing


@pytest.mark.parametrize("raw,expected", [
    ("https://doi.org/10.1101/LM.488707", "10.1101/lm.488707"),
    ("doi:10.1037/a0017407", "10.1037/a0017407"),
    ("http://dx.doi.org/10.1/x", "10.1/x"),
    ("not a doi", ""),
    (None, ""),
])
def test_normalize_doi(raw, expected):
    assert P.normalize_doi(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1706.03762v7", "1706.03762"),
    ("arXiv:1706.03762", "1706.03762"),
    ("https://arxiv.org/abs/2502.06192v2", "2502.06192"),
    ("https://arxiv.org/pdf/2502.06192v2.pdf", "2502.06192"),
    ("10.48550/arXiv.1706.03762", "1706.03762"),
    ("hep-th/9901001v1", "hep-th/9901001"),
    ("http://arxiv.org/api/errors#incorrect_id_format_for_x", ""),
])
def test_normalize_arxiv_id(raw, expected):
    assert P.normalize_arxiv_id(raw) == expected


# ---------------------------------------------------------------- parsing


def test_openalex_record_parses_and_a_journal_venue_wins_over_the_type_word():
    p = P.parse_openalex_work(OPENALEX_WORK)
    assert p.title.startswith("Neurogenesis and the spacing effect")
    assert p.authors == ("Helene M. Sisti", "Tracey J. Shors")
    assert (p.year, p.doi, p.openalex_id) == (2007, "10.1101/lm.488707", "W2167585898")
    assert p.abstract == "Information that is spaced over time is better remembered"
    assert (p.volume, p.issue, p.pages) == ("14", "5", "368-375")
    assert p.oa_pdf_urls == ("http://learnmem.cshlp.org/content/14/5/368.full.pdf",)
    # OpenAlex typed this journal article "preprint" (CSHL shares bioRxiv's DOI
    # prefix); the venue it appeared in is the observable fact.
    assert p.work_type == "preprint"
    assert p.status == "journal"
    assert p.key == "doi:10.1101/lm.488707"


def test_openalex_arxiv_datacite_doi_becomes_an_arxiv_id():
    w = dict(OPENALEX_WORK, doi="https://doi.org/10.48550/arxiv.1706.03762",
             ids={"doi": "https://doi.org/10.48550/arxiv.1706.03762"},
             primary_location={"source": {"display_name": "arXiv (Cornell University)", "type": "repository"}})
    p = P.parse_openalex_work(w)
    assert (p.doi, p.arxiv_id, p.status) == ("", "1706.03762", "preprint")


def test_arxiv_feed_parses_entries_and_skips_the_error_entry():
    got = P.parse_arxiv_feed(ARXIV_FEED)
    assert len(got) == 1
    p = got[0]
    assert (p.arxiv_id, p.year, p.status) == ("2502.06192", 2025, "preprint")
    assert p.oa_pdf_urls == ("https://arxiv.org/pdf/2502.06192",)
    assert p.authors == ("Guanglong Sun", "Hongwei Yan")


def test_arxiv_feed_refuses_entity_expansion():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&lol2;</title></feed>')
    with pytest.raises(Exception):  # noqa: B017 -- defusedxml raises its own types
        P.parse_arxiv_feed(bomb)


def test_s2_record_parses():
    p = P.parse_s2_paper(S2_PAPER)
    assert (p.doi, p.arxiv_id, p.venue_type, p.status) == ("10.5555/3295222.3295349", "1706.03762", "journal", "journal")
    assert p.pages == "5998-6008"
    assert p.cited_by == 100000
    assert p.oa_pdf_urls == ("https://example.org/attention.pdf",)


# ---------------------------------------------------------------- merging


def test_the_same_work_from_three_services_is_one_paper():
    oa = P.parse_openalex_work(dict(OPENALEX_WORK, cited_by_count=150))
    s2 = P.Paper(title=oa.title, authors=("H. Sisti",), doi="10.1101/lm.488707", arxiv_id="0705.00001",
                 cited_by=194, s2_id="abc", found_by=(P.SEMANTIC_SCHOLAR,))
    ax = P.Paper(title="A different title of the preprint version", arxiv_id="0705.00001",
                 venue_type="repository", oa_pdf_urls=("https://arxiv.org/pdf/0705.00001",), found_by=(P.ARXIV,))
    merged = P.merge_papers([oa, s2, ax])
    assert len(merged) == 1
    m = merged[0]
    assert m.found_by == (P.OPENALEX, P.SEMANTIC_SCHOLAR, P.ARXIV)
    assert (m.doi, m.arxiv_id, m.s2_id, m.openalex_id) == ("10.1101/lm.488707", "0705.00001", "abc", "W2167585898")
    assert m.cited_by == 194
    assert m.status == "journal"
    assert m.oa_pdf_urls[0] == "https://arxiv.org/pdf/0705.00001"
    assert "http://learnmem.cshlp.org/content/14/5/368.full.pdf" in m.oa_pdf_urls


def test_title_and_first_author_join_records_without_ids_but_short_titles_do_not():
    a = P.Paper(title="Spacing effects in learning: a temporal ridgeline", authors=("Nicholas J. Cepeda",))
    b = P.Paper(title="Spacing Effects in Learning - A Temporal Ridgeline", authors=("N Cepeda",), year=2008)
    c = P.Paper(title="Introduction", authors=("A Smith",))
    d = P.Paper(title="Introduction", authors=("A Smith",))
    merged = P.merge_papers([a, b, c, d])
    assert len(merged) == 3
    assert merged[0].year == 2008


def test_two_published_dois_under_one_arxiv_id_keep_the_journal_one():
    conf = P.Paper(title="X", doi="10.1/conf", arxiv_id="1234.56789", venue_type="conference")
    jour = P.Paper(title="X", doi="10.1/jour", arxiv_id="1234.56789", venue_type="journal")
    (m,) = P.merge_papers([conf, jour])
    assert (m.doi, m.other_dois) == ("10.1/jour", ("10.1/conf",))


def test_paper_round_trips_through_a_dict():
    p = P.parse_openalex_work(OPENALEX_WORK)
    assert P.Paper.from_dict(json.loads(json.dumps(p.to_dict()))) == p


# ---------------------------------------------------------------- transport


def test_throttle_spaces_requests():
    now = [100.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    t = P.Throttle(1.0, clock=lambda: now[0], sleep=sleep)
    t.wait()
    t.wait()
    now[0] += 5
    t.wait()
    assert slept == [1.0]


def test_service_intervals_are_one_second_and_three_for_arxiv():
    assert P.MIN_INTERVAL == {P.OPENALEX: 1.0, P.SEMANTIC_SCHOLAR: 1.0, P.ARXIV: 3.0, P.WIKIPEDIA: 1.0}
    assert P.THROTTLES[P.ARXIV].interval == 3.0


def test_a_429_is_waited_out_then_retried(tmp_path):
    client, transport, sleeps = _client(tmp_path, [
        (429, {"Retry-After": "2"}, b""), _ok({"results": [OPENALEX_WORK]}),
    ])
    got = client.openalex_search("spacing effect")
    assert len(got) == 1 and sleeps == [2.0]
    assert client.requests_sent[P.OPENALEX] == 2


def test_a_long_retry_after_is_reported_not_slept(tmp_path):
    client, _, sleeps = _client(tmp_path, [(429, {"Retry-After": "600"}, b"")])
    with pytest.raises(P.PaperSourceError) as exc:
        client.openalex_search("spacing effect")
    assert exc.value.status == 429 and sleeps == []
    assert "no API key" in str(exc.value)


def test_three_429s_give_up_with_the_reason(tmp_path):
    client, transport, sleeps = _client(tmp_path, [(429, {}, b"")] * 3, s2_key="k")
    with pytest.raises(P.PaperSourceError) as exc:
        client.s2_search("spacing effect")
    assert len(transport.calls) == 3 and len(sleeps) == 2
    assert "rate limited" in str(exc.value) and "no API key" not in str(exc.value)


def test_http_and_network_errors_name_the_service(tmp_path):
    client, _, _ = _client(tmp_path, [(500, {}, b"boom"), OSError("down")])
    with pytest.raises(P.PaperSourceError, match="OpenAlex: HTTP 500: boom"):
        client.openalex_search("a")
    with pytest.raises(P.PaperSourceError, match="Semantic Scholar: OSError: down"):
        client.s2_search("a")


def test_answers_are_cached_without_the_api_key(tmp_path):
    client, transport, _ = _client(tmp_path, [_ok({"results": [OPENALEX_WORK]})], openalex_key="SECRET123")
    client.openalex_search("spacing effect")
    url = transport.calls[0][0]
    assert "api_key=SECRET123" in url
    # Another key, same question: answered from the cache, and the key is in no cache file.
    other, transport2, _ = _client(tmp_path, [], openalex_key="OTHER")
    assert len(other.openalex_search("spacing effect")) == 1
    assert transport2.calls == []
    for f in (tmp_path / "cache").rglob("*.json"):
        assert "SECRET123" not in f.read_text()


def test_an_expired_cache_entry_is_fetched_again(tmp_path):
    now = [1000.0]
    client, transport, _ = _client(tmp_path, [_ok({"results": []}), _ok({"results": []})], clock=lambda: now[0])
    client.openalex_search("a b")
    now[0] += P.CACHE_TTL + 1
    client.openalex_search("a b")
    assert len(transport.calls) == 2


def test_keys_go_where_each_service_wants_them(tmp_path):
    client, transport, _ = _client(tmp_path, [_ok({"data": []})], s2_key="S2KEY")
    client.s2_search("x")
    assert transport.calls[0][1]["x-api-key"] == "S2KEY"
    keyless, t2, _ = _client(tmp_path, [_ok({"results": []}), _ok({"data": []})])
    keyless.openalex_search("y")
    keyless.s2_search("y")
    assert "api_key" not in t2.calls[0][0]
    assert "x-api-key" not in t2.calls[1][1]


def test_requests_carry_nothing_about_the_owner(tmp_path):
    client, transport, _ = _client(tmp_path, [_ok({"results": []}), _ok({"data": []}), _ok(b"<feed xmlns='http://www.w3.org/2005/Atom'/>")])
    client.search("memory consolidation sleep")
    for url, headers in transport.calls:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert not ({"mailto", "email"} & set(q))
        assert headers["User-Agent"] == "dourmouse-research/1.0"


def test_search_reports_a_failing_service_and_keeps_the_others(tmp_path):
    client, _, _ = _client(tmp_path, [
        _ok({"results": [OPENALEX_WORK]}),
        (429, {"Retry-After": "999"}, b""),
        _ok(ARXIV_FEED),
    ])
    r = client.search("spacing effect")
    assert len(r.papers) == 2
    assert r.counts == {P.OPENALEX: 1, P.ARXIV: 1}
    assert P.SEMANTIC_SCHOLAR in r.errors and "429" in r.errors[P.SEMANTIC_SCHOLAR]
    assert "failed: Semantic Scholar" in r.note()


def test_top_reviews_asks_for_reviews_by_citations_in_title_and_abstract(tmp_path):
    review = dict(OPENALEX_WORK, type="review", id="https://openalex.org/W1", doi="https://doi.org/10.1/r1")
    # Distinct titles: identical title and first author would (correctly) merge them.
    client, transport, _ = _client(tmp_path, [_ok({"results": [
        review,
        dict(review, id="W2", doi="10.1/r2", display_name="Distributed practice in verbal recall tasks: a review"),
        dict(review, id="W3", doi="10.1/r3", display_name="Spacing and retrieval practice in classrooms: a review"),
    ]})])
    r = client.top_reviews("spacing effect, learning", n=2)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
    assert q["filter"] == ["type:review,title_and_abstract.search:spacing effect  learning"]
    assert q["sort"] == ["cited_by_count:desc"]
    assert len(r.papers) == 2 and all(p.is_review for p in r.papers)


def test_arxiv_query_keeps_a_few_content_words(tmp_path):
    client, transport, _ = _client(tmp_path, [_ok(ARXIV_FEED)])
    client.arxiv_search("Does the spacing effect improve the long term retention of vocabulary in students")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
    assert q["search_query"] == ["all:spacing AND all:effect AND all:improve AND all:long AND all:term"]


def test_s2_neighbours_reads_references_and_citations(tmp_path):
    client, transport, _ = _client(tmp_path, [
        _ok({"data": [{"citedPaper": S2_PAPER}, {"citedPaper": {"title": None}}]}),
        _ok({"data": [{"citingPaper": S2_PAPER}]}),
    ])
    p = P.Paper(title="t", doi="10.1/x")
    assert len(client.s2_neighbours(p, direction="references")) == 1
    assert len(client.s2_neighbours(p, direction="citations")) == 1
    assert "/paper/DOI:10.1/x/references" in transport.calls[0][0]
    assert client.s2_neighbours(P.Paper(title="no ids")) == []


def test_openalex_lookup_by_doi_and_a_missing_work(tmp_path):
    client, transport, _ = _client(tmp_path, [_ok(OPENALEX_WORK), (404, {}, b"not found")])
    assert client.openalex_work(doi="https://doi.org/10.1101/LM.488707").title.startswith("Neurogenesis")
    assert "/works/doi:10.1101/lm.488707" in transport.calls[0][0]
    assert client.openalex_work(doi="10.1/none") is None


# ---------------------------------------------------------------- ranking


def test_relevance_beats_raw_citations():
    on_topic = P.Paper(title="The spacing effect in vocabulary learning", abstract="spaced practice improves retention",
                       year=2015, cited_by=50, venue_type="journal")
    famous = P.Paper(title="Deep residual learning for image recognition", abstract="residual networks",
                     year=2016, cited_by=200000, venue_type="conference")
    ranked = P.rank_papers([famous, on_topic], P.query_terms("spacing effect vocabulary retention"), now_year=2026)
    assert ranked[0][0] is on_topic
    assert 0 < ranked[0][1].relevance <= 1


def test_a_journal_outranks_the_same_preprint_and_ranking_is_deterministic():
    base = {"title": "Spacing effect in learning", "abstract": "spacing effect", "year": 2020, "cited_by": 10}
    j = P.Paper(**base, venue_type="journal", doi="10.1/j")
    pre = P.Paper(**base, venue_type="repository", arxiv_id="2001.00001")
    terms = P.query_terms("spacing effect")
    first = [p.key for p, _ in P.rank_papers([pre, j], terms, now_year=2026)]
    again = [p.key for p, _ in P.rank_papers([j, pre], terms, now_year=2026)]
    assert first == again == ["doi:10.1/j", "arxiv:2001.00001"]


def test_wikipedia_article_is_found_then_read_as_plain_text(tmp_path):
    client, transport, _ = _client(tmp_path, [
        _ok({"query": {"search": [{"title": "Spacing effect"}]}}),
        _ok({"query": {"pages": {"1": {"title": "Spacing effect", "extract": "The spacing effect is ..."}}}}),
    ])
    title, url, text = client.wikipedia_article("spacing effect")
    assert (title, url, text) == ("Spacing effect", "https://en.wikipedia.org/wiki/Spacing_effect", "The spacing effect is ...")
    assert "explaintext=1" in transport.calls[1][0]
    empty, _, _ = _client(tmp_path / "b", [_ok({"query": {"search": []}})])
    assert empty.wikipedia_article("zzqx") is None
