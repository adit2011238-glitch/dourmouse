"""Main-content extraction from HTML (R0-1, finding #091). Stdlib only.

The old path (regex strip) kept everything that was not a tag: navigation,
headers, footers, sidebars, cookie banners, share bars. So "evidence" could
be a site's own menu, and a claim's location was a model's guess, because
the text carried no structure at all.

This parses the page into a small element tree, removes boilerplate
(semantic elements such as <nav>/<footer>/<aside>, ARIA landmark roles, and
class/id names that say menu, cookie, share and so on), picks the main
content (an <article> or <main> when the page marks one, otherwise the
container with the best readability-style paragraph score, penalised by link
density), and serialises it as blocks that carry their heading path. That
heading path is what lets a claim say "under Architecture > Transport"
instead of "second paragraph".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
_DROP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "nav", "header", "footer", "aside", "form", "button", "select", "dialog", "menu",
}
_DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "dialog", "alert"}
_BOILERPLATE_RE = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|footer|header|masthead|sidebar|side-bar|breadcrumbs?|"
    r"cookies?|consent|gdpr|banner|advert|ads?|promo|sponsor|share|sharing|social|"
    r"comments?|related|recommend|newsletter|subscribe|signup|popup|modal|toolbar|"
    r"skip-link|pagination|pager|headerlink|anchor-?link)($|[\s_-])",
    re.IGNORECASE,
)
_BLOCKS = {"p", "pre", "li", "blockquote", "dd", "dt", "figcaption", "td", "th", "caption"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Tags an unclosed <p>/<li> implicitly ends at (HTML's optional end tags).
_IMPLICIT_CLOSE = {
    "p": {"p", "div", "ul", "ol", "table", "pre", "blockquote", "section", "article",
          "h1", "h2", "h3", "h4", "h5", "h6", "form", "figure", "dl", "main"},
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "td": {"td", "th", "tr"},
    "th": {"td", "th", "tr"},
    "tr": {"tr"},
}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        out: list[str] = []
        stack: list[_Node | str] = [self]
        while stack:
            cur = stack.pop()
            if isinstance(cur, str):
                out.append(cur)
            else:
                stack.extend(reversed(cur.children))
        return " ".join("".join(out).split())

    def link_text_len(self) -> int:
        total = 0
        stack: list[_Node | str] = [self]
        while stack:
            cur = stack.pop()
            if isinstance(cur, _Node):
                if cur.tag == "a":
                    total += len(cur.text())
                else:
                    stack.extend(cur.children)
        return total

    def iter_nodes(self):  # type: ignore[no-untyped-def]
        stack: list[_Node] = [self]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(c for c in reversed(cur.children) if isinstance(c, _Node))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.cur = self.root
        self.title_parts: list[str] = []
        self._in_title = False
        self._title_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        # Only the document's first <title>: an inline <svg> in the body has
        # its own <title> elements (live on bbc.com they were appended to the
        # page title). Those stay in the tree and are dropped with the svg.
        if tag == "title" and not self._title_done:
            self._in_title = True
            return
        closers = {t for t, stops in _IMPLICIT_CLOSE.items() if tag in stops}
        while self.cur.tag in closers and self.cur.parent is not None:
            self.cur = self.cur.parent
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.cur.children.append(_Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.cur))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
            return
        node: _Node | None = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent  # stray end tags with no open match are ignored

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.cur.children.append(data)


def _is_boilerplate(node: _Node, inside_content: bool = False, page_chars: int = 0) -> bool:
    # An article's own <header>/<footer> holds its title and byline, not
    # site chrome: only drop them outside <article>/<main>.
    if node.tag in ("header", "footer") and inside_content:
        return False
    if node.tag in _DROP_TAGS:
        return True
    a = node.attrs
    if a.get("role", "").lower() in _DROP_ROLES:
        return True
    if "hidden" in a or a.get("aria-hidden", "").lower() == "true":
        return True
    style = a.get("style", "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    ident = f"{a.get('id', '')} {a.get('class', '')}"
    # Never drop the element that IS the main content just because its
    # class also mentions something like "article-header".
    if node.tag in ("article", "main", "body") or not _BOILERPLATE_RE.search(ident):
        return False
    # A class name is a hint, not proof: live on github.com the page-wide
    # wrapper <div> carried "header-overlay" and took the whole page with it.
    # A real menu, sidebar or footer never holds most of a page's text.
    return not page_chars or len(node.text()) < 0.4 * page_chars


def _prune(node: _Node, inside_content: bool = False, page_chars: int | None = None) -> None:
    if page_chars is None:
        page_chars = len(node.text())
    inside = inside_content or node.tag in ("article", "main") or node.attrs.get("role") == "main"
    node.children = [
        c for c in node.children if isinstance(c, str) or not _is_boilerplate(c, inside, page_chars)
    ]
    for c in node.children:
        if isinstance(c, _Node):
            _prune(c, inside, page_chars)


def _pick_main(body: _Node) -> _Node:
    marked = [n for n in body.iter_nodes() if n.tag in ("article", "main") or n.attrs.get("role") == "main"]
    if marked:
        best = max(marked, key=lambda n: len(n.text()))
        if len(best.text()) >= 200:
            return best
    scores: dict[int, float] = {}
    by_id: dict[int, _Node] = {}
    for n in body.iter_nodes():
        if n.tag not in ("p", "pre", "td", "blockquote"):
            continue
        t = n.text()
        if len(t) < 25:
            continue
        s = 1.0 + t.count(",") + min(len(t) // 100, 3)
        parent, grand = n.parent, n.parent.parent if n.parent else None
        for anc, weight in ((parent, 1.0), (grand, 0.5)):
            if anc is not None:
                by_id[id(anc)] = anc
                scores[id(anc)] = scores.get(id(anc), 0.0) + s * weight
    if not scores:
        return body
    for k, anc in by_id.items():
        total = len(anc.text()) or 1
        scores[k] *= 1.0 - min(anc.link_text_len() / total, 1.0)
    best_node = by_id[max(scores, key=lambda k: scores[k])]
    # A best container holding only a sliver of the page's prose usually
    # means the content is spread across siblings: keep the whole body.
    if len(best_node.text()) < 0.25 * len(body.text()):
        return body
    return best_node


@dataclass(frozen=True)
class Block:
    heading_path: tuple[str, ...]
    kind: str  # "heading" | "paragraph" | "list-item" | "pre" | "quote" | "cell"
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    blocks: tuple[Block, ...]

    @property
    def text(self) -> str:
        out: list[str] = []
        for b in self.blocks:
            if b.kind == "heading":
                level = len(b.heading_path)
                out.append(("#" * max(level, 1)) + " " + b.text)
            elif b.kind == "list-item":
                out.append("- " + b.text)
            else:
                out.append(b.text)
        return "\n\n".join(out)

    def locate(self, passage: str) -> str | None:
        """Where a (whitespace-normalised) passage sits, as the heading path
        of the first block containing it, e.g. "Architecture > Transport,
        paragraph 2". None when the passage spans blocks or is absent."""
        needle = " ".join(passage.split())
        counts: dict[tuple[str, ...], int] = {}
        for b in self.blocks:
            if b.kind == "heading":
                continue
            counts[b.heading_path] = counts.get(b.heading_path, 0) + 1
            if needle and needle in " ".join(b.text.split()):
                where = " > ".join(b.heading_path) if b.heading_path else "before the first heading"
                return f"{where}, paragraph {counts[b.heading_path]}"
        return None


def _serialise(root: _Node) -> list[Block]:
    blocks: list[Block] = []
    path: list[tuple[int, str]] = []

    def heading_path() -> tuple[str, ...]:
        return tuple(t for _, t in path)

    def walk(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                t = " ".join(child.split())
                if t and node.tag not in _BLOCKS and node.tag not in _HEADINGS:
                    blocks.append(Block(heading_path(), "loose", t))
                continue
            tag = child.tag
            if tag in _HEADINGS:
                t = child.text()
                if t:
                    level = int(tag[1])
                    while path and path[-1][0] >= level:
                        path.pop()
                    path.append((level, t))
                    blocks.append(Block(heading_path(), "heading", t))
            elif tag == "pre":
                raw = _raw_text(child).strip("\n")
                if raw.strip():
                    blocks.append(Block(heading_path(), "pre", raw))
            elif tag in _BLOCKS:
                if any(isinstance(c, _Node) and (c.tag in _BLOCKS or c.tag in _HEADINGS) for c in child.iter_nodes() if c is not child):
                    walk(child)  # e.g. an <li> holding <p>s: emit the inner blocks
                    continue
                t = child.text()
                if t:
                    kind = {"li": "list-item", "blockquote": "quote", "td": "cell", "th": "cell"}.get(tag, "paragraph")
                    blocks.append(Block(heading_path(), kind, t))
            elif tag == "br":
                continue
            else:
                walk(child)

    walk(root)
    # Loose text (not inside any block element) split by inline tags such as
    # <a> or <b> is one run of prose: join adjacent loose fragments, then
    # present them as paragraphs. Real <p> blocks are never merged.
    merged: list[Block] = []
    for b in blocks:
        if merged and b.kind == "loose" and merged[-1].kind == "loose" and merged[-1].heading_path == b.heading_path:
            merged[-1] = Block(b.heading_path, "loose", merged[-1].text + " " + b.text)
        else:
            merged.append(b)
    return [Block(b.heading_path, "paragraph", b.text) if b.kind == "loose" else b for b in merged]


def _raw_text(node: _Node) -> str:
    out: list[str] = []
    stack: list[_Node | str] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        else:
            if cur.tag == "br":
                out.append("\n")
            stack.extend(reversed(cur.children))
    return "".join(out)


def extract_main(markup: str) -> ExtractedDocument:
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    root = builder.root
    body = next((n for n in root.iter_nodes() if n.tag == "body"), root)
    _prune(body)
    main = _pick_main(body)
    blocks = _serialise(main)
    title = " ".join("".join(builder.title_parts).split())
    if not title:
        h1 = next((n for n in root.iter_nodes() if n.tag == "h1"), None)
        title = h1.text() if h1 else ""
    return ExtractedDocument(title=title, blocks=tuple(blocks))
