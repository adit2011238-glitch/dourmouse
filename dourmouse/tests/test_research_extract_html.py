"""Finding #091 (R0-1): main-content extraction keeps the article, drops the
site chrome, and records where each block sits under the headings."""

from __future__ import annotations

from dourmouse.research_pipeline.extract_html import extract_main

_NEWS_PAGE = """<!doctype html>
<html><head><title>MCP explained | Example News</title>
<style>.x{color:red}</style><script>track()</script></head>
<body>
<div id="cookie-banner">We use cookies. <button>Accept all</button></div>
<header class="site-header"><a href="/">Example News</a>
  <nav><ul><li><a href="/world">World</a></li><li><a href="/tech">Tech</a></li></ul></nav>
</header>
<div class="layout">
  <aside class="sidebar"><h3>Trending</h3><ul><li><a href="/a">Celebrity story</a></li></ul></aside>
  <article>
    <header><h1>What MCP actually is</h1><p class="byline">By A. Writer</p></header>
    <p>The Model Context Protocol connects language models to tools, and it does so
       through a small, well-defined set of messages.</p>
    <h2>Architecture</h2>
    <p>MCP has three core parts: hosts, servers, and clients.</p>
    <h3>Transport</h3>
    <p>Messages travel over stdio or over HTTP with server-sent events.</p>
    <ul><li>stdio for local servers</li><li>HTTP for remote ones</li></ul>
    <pre>{"jsonrpc": "2.0",
  "method": "tools/list"}</pre>
    <div class="share-bar"><a href="#">Share on X</a> <a href="#">Share on Facebook</a></div>
  </article>
</div>
<div class="related-stories"><h2>Related</h2><p><a href="/b">Another story you might like</a></p></div>
<footer><p>Copyright Example News. All rights reserved.</p></footer>
</body></html>"""


def test_the_article_is_kept_and_the_chrome_is_dropped():
    doc = extract_main(_NEWS_PAGE)
    text = doc.text
    for kept in ("What MCP actually is", "three core parts: hosts, servers, and clients",
                 "stdio or over HTTP", "stdio for local servers", "tools/list"):
        assert kept in text
    for dropped in ("cookies", "Accept all", "World", "Trending", "Celebrity",
                    "Share on", "Another story", "Copyright", "track()", "color:red"):
        assert dropped not in text


def test_the_title_comes_from_the_title_element():
    assert extract_main(_NEWS_PAGE).title == "MCP explained | Example News"


def test_headings_nest_into_a_path():
    blocks = extract_main(_NEWS_PAGE).blocks
    transport = next(b for b in blocks if "stdio or over HTTP" in b.text)
    assert transport.heading_path == ("What MCP actually is", "Architecture", "Transport")


def test_a_passage_is_located_by_heading_path_not_guessed():
    doc = extract_main(_NEWS_PAGE)
    assert doc.locate("MCP has three core parts:   hosts, servers, and clients.") == (
        "What MCP actually is > Architecture, paragraph 1"
    )
    assert doc.locate("never on the page") is None


def test_list_items_and_preformatted_text_keep_their_shape():
    doc = extract_main(_NEWS_PAGE)
    assert "- stdio for local servers" in doc.text
    pre = next(b for b in doc.blocks if b.kind == "pre")
    assert '"jsonrpc": "2.0",\n' in pre.text


def test_without_article_markup_the_densest_prose_wins():
    page = """<html><body>
    <div class="menu"><a href="/1">Home</a> <a href="/2">About</a> <a href="/3">Blog</a></div>
    <div id="links"><p><a href="/x">A long list of links that is mostly links, links, links</a></p></div>
    <div id="content">
      <p>First real paragraph, with commas, clauses, and enough words to count as prose here.</p>
      <p>Second real paragraph, again with commas, clauses, and a sentence that carries meaning.</p>
      <p>Third real paragraph, which, like the others, is plain body text rather than navigation.</p>
    </div></body></html>"""
    text = extract_main(page).text
    assert "First real paragraph" in text and "Third real paragraph" in text
    assert "Home" not in text and "mostly links" not in text


def test_hidden_elements_are_never_evidence():
    page = ('<html><body><main><p>Visible text that is long enough to be the main content of this page '
            'and then some more words to pass the length floor for a marked main element easily.</p>'
            '<p style="display: none">Hidden seo text</p><p hidden>Also hidden</p>'
            '<p aria-hidden="true">Screen reader hidden</p></main></body></html>')
    text = extract_main(page).text
    assert "Visible text" in text
    assert "Hidden seo" not in text and "Also hidden" not in text and "Screen reader hidden" not in text


def test_unclosed_paragraphs_and_stray_end_tags_do_not_break_parsing():
    page = "<body><article><h1>T</h1><p>one<p>two</div><p>three</article></body>"
    blocks = [b.text for b in extract_main(page).blocks if b.kind == "paragraph"]
    assert blocks == ["one", "two", "three"]


def test_entities_are_decoded():
    page = "<body><main><p>caf&eacute; &mdash; &#8220;quoted&#8221; &amp; more</p></main></body>"
    assert "café — “quoted” & more" in extract_main(page).text


def test_svg_titles_in_the_body_never_join_the_page_title():
    """Live on bbc.com: inline <svg><title>...</title></svg> logos were
    appended to the document title."""
    page = ("<html><head><title>Real title</title></head><body>"
            "<svg><title>Logo name</title></svg><main><p>" + "Body text. " * 30 + "</p></main></body></html>")
    doc = extract_main(page)
    assert doc.title == "Real title"
    assert "Logo name" not in doc.text


def test_sphinx_heading_anchors_are_dropped():
    page = ('<html><body><main><h1>Parser<a class="headerlink" href="#p">¶</a></h1>'
            "<p>" + "Words here. " * 30 + "</p></main></body></html>")
    assert extract_main(page).blocks[0].text == "Parser"


def test_a_page_wide_wrapper_with_a_chrome_class_is_not_dropped():
    """Live on github.com: <div class="... header-overlay ..."> wrapped the
    whole page and the class heuristic removed everything."""
    page = ('<html><body><div class="logged-out header-overlay"><div class="menu"><a href="/">Home</a></div>'
            "<div><p>" + "Real content sentence, with commas, and length. " * 20 + "</p></div></div></body></html>")
    text = extract_main(page).text
    assert "Real content sentence" in text
    assert "Home" not in text
