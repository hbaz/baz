"""Kind ``html_list``: an agency newsroom read as a list page plus article pages.

Standard library only. The page is parsed with ``html.parser`` into a small
element tree, and the source file's ``extract:`` rules pick the parts:

    extract:
      list:
        item_selector:  one entry per release on the list page
        link_selector:  the link inside an entry (``link_attr``, default href)
        title_selector: the entry's title text
        date_selector:  the entry's date (a ``datetime`` attribute wins over text)
        date_formats:   strptime formats tried on the date text
        same_host_only: follow only links on the source's declared hosts
      article:
        title_selector, date_selector, body_selector, date_formats
      required_fields:  fields every record must have (default title, url,
                        published_on, body)

Selectors are a CSS subset: type, ``.class``, ``#id``, ``[attr]`` and
``[attr=value]`` in one compound, the descendant (space) and child (``>``)
combinators, and comma-separated alternatives. A selector outside the subset
is a recorded source failure, not a guess.

Failures are recorded, never skipped silently (each becomes an entry in
``Batch.errors``):
  - the list page answers but the item selector matches nothing: stage
    ``page``, "zero matches" (the usual sign of a changed layout);
  - an entry with no link, a link to an undeclared host, or an article page
    that fails to load: stage ``list`` or ``article``;
  - an article missing a required field (no title, no body, no parsable
    date): stage ``article``, "malformed", and the record is not kept.

Paging: only the first list page is read. When every entry on it falls
inside the window, older in-window entries may sit on the next page, so the
batch is marked ``truncated``. Following list pages is left to the shared
paging primitive (T236, ``base.walk_pages``) rather than a fourth loop here.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import re
import urllib.parse
from html.parser import HTMLParser

from . import base

DEFAULT_REQUIRED = ("title", "url", "published_on", "body")
DEFAULT_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y")


class SelectorError(base.SourceError):
    """An extraction rule uses a selector outside the supported subset."""


# ---------------------------------------------------------------------------
# A small element tree built with html.parser


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    @property
    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        """Descendant elements in document order (not the node itself)."""
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def text(self) -> str:
        """Block-aware text: one line per block element, whitespace collapsed."""
        parts = []
        _collect_text(self, parts)
        lines = [" ".join(line.split()) for line in "".join(parts).splitlines()]
        return "\n".join(line for line in lines if line)

    def inline_text(self) -> str:
        return " ".join(self.text().split())

    def __repr__(self):
        return f"<Node {self.tag} {self.attrs}>"


_SKIP = {"script", "style", "noscript", "template", "head"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
          "article", "header", "footer", "blockquote", "table", "main", "nav", "aside", "dd", "dt"}


def _collect_text(node: Node, parts: list):
    for c in node.children:
        if isinstance(c, str):
            parts.append(c)
        elif c.tag in _SKIP:
            continue
        else:
            block = c.tag in _BLOCK
            if block:
                parts.append("\n")
            _collect_text(c, parts)
            if block:
                parts.append("\n")


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}
# A new one of these closes an open one of the same tag (HTML's implied end tags).
_SELF_CLOSING_SIBLINGS = {"li", "dt", "dd", "tr", "td", "th", "option", "p"}
_CLOSES_P = {"address", "article", "aside", "blockquote", "div", "dl", "footer", "form", "h1", "h2",
             "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section",
             "table", "ul"}


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        top = self.stack[-1]
        if top.tag == "p" and tag in _CLOSES_P:
            self.stack.pop()
        elif tag in _SELF_CLOSING_SIBLINGS and top.tag == tag:
            self.stack.pop()
        parent = self.stack[-1]
        node = Node(tag, [(k, v if v is not None else "") for k, v in attrs], parent)
        parent.children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        parent = self.stack[-1]
        parent.children.append(Node(tag, [(k, v if v is not None else "") for k, v in attrs], parent))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # A stray end tag: ignored, as browsers do.

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    b = _TreeBuilder()
    b.feed(html)
    b.close()
    return b.root


# ---------------------------------------------------------------------------
# Selectors (a CSS subset)

_COMPOUND = re.compile(
    r"""^(?P<tag>[a-zA-Z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+|\[[\w-]+(?:=(?:"[^"]*"|'[^']*'|[^\]"']*))?\])*)$"""
)
_PART = re.compile(r"""[.#][\w-]+|\[([\w-]+)(?:=("[^"]*"|'[^']*'|[^\]"']*))?\]""")


def _parse_compound(s: str):
    m = _COMPOUND.match(s)
    if not m or not s:
        raise SelectorError(f"unsupported selector part {s!r}")
    tag = (m.group("tag") or "*").lower()
    classes, ids, attrs = [], [], []
    for p in _PART.finditer(m.group("rest")):
        tok = p.group(0)
        if tok[0] == ".":
            classes.append(tok[1:])
        elif tok[0] == "#":
            ids.append(tok[1:])
        else:
            v = p.group(2)
            if v is not None and v[:1] in "\"'":
                v = v[1:-1]
            attrs.append((p.group(1).lower(), v))
    return (tag, classes, ids, attrs)


def parse_selector(selector) -> list:
    """Parse into alternatives; each is a list of (combinator, compound), left to right."""
    if not isinstance(selector, str) or not selector.strip():
        raise SelectorError(f"empty or non-text selector {selector!r}")
    alts = []
    for alt in selector.split(","):
        toks = alt.replace(">", " > ").split()
        if not toks:
            raise SelectorError(f"empty alternative in selector {selector!r}")
        seq, comb = [], " "
        for t in toks:
            if t == ">":
                if not seq or comb == ">":
                    raise SelectorError(f"misplaced '>' in selector {selector!r}")
                comb = ">"
                continue
            seq.append((comb, _parse_compound(t)))
            comb = " "
        if comb == ">":
            raise SelectorError(f"selector {selector!r} ends with '>'")
        alts.append(seq)
    return alts


def _match_compound(node: Node, comp) -> bool:
    tag, classes, ids, attrs = comp
    if tag != "*" and node.tag != tag:
        return False
    if classes and not set(classes) <= node.classes:
        return False
    for i in ids:
        if node.attrs.get("id") != i:
            return False
    for k, v in attrs:
        if k not in node.attrs or (v is not None and node.attrs[k] != v):
            return False
    return True


def _match_seq(node: Node, seq, i: int, scope: Node) -> bool:
    """Does ``node`` match seq[i], with seq[:i] matched by ancestors inside ``scope``?"""
    if not _match_compound(node, seq[i][1]):
        return False
    if i == 0:
        return True
    comb = seq[i][0]
    for p in _ancestors(node, scope):
        if _match_seq(p, seq, i - 1, scope):
            return True
        if comb == ">":
            return False
    return False


def _ancestors(node: Node, scope: Node):
    """Ancestors of ``node``, nearest first, stopping below ``scope``."""
    chain = []
    p = node.parent
    for _ in range(10000):  # a depth bound; html.parser trees are far shallower
        if p is None or p is scope:
            break
        chain.append(p)
        p = p.parent
    return chain


def select(scope: Node, selector) -> list:
    """Elements under ``scope`` (not scope itself) matching the selector, in document order."""
    alts = parse_selector(selector) if not isinstance(selector, list) else selector
    return [n for n in scope.iter() if any(_match_seq(n, seq, len(seq) - 1, scope) for seq in alts)]


def select_first(scope: Node, selector):
    """First match of the first alternative that matches (alternatives are in priority order)."""
    for seq in parse_selector(selector):
        for n in scope.iter():
            if _match_seq(n, seq, len(seq) - 1, scope):
                return n
    return None


# ---------------------------------------------------------------------------
# Dates

_DATE_CANDIDATES = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}"
)


def parse_date(text, formats=DEFAULT_DATE_FORMATS):
    """An ISO date string, or None. Tries the whole text, then date-shaped substrings."""
    if not isinstance(text, str):
        return None
    s = " ".join(text.split())
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$", s)
    if m:
        try:
            return _dt.date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None
    for cand in [s] + _DATE_CANDIDATES.findall(s):
        cand = cand.replace(".", "") if re.match(r"[A-Z][a-z]{2}\.", cand) else cand
        for fmt in formats or DEFAULT_DATE_FORMATS:
            try:
                return _dt.datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    try:  # RFC 822 (RSS pubDate)
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def node_date(node, formats):
    if node is None:
        return None
    for attr in ("datetime", "content"):
        d = parse_date(node.attrs.get(attr), formats)
        if d:
            return d
    return parse_date(node.inline_text(), formats)


def in_window(iso: str, window: base.Window) -> bool:
    d = _dt.date.fromisoformat(iso)
    return window.start <= d <= window.end


# ---------------------------------------------------------------------------
# Extraction shared with kind rss


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_article(html: str, rules: dict) -> dict:
    """Title, published_on and body from an article page by the ``article`` rules."""
    root = parse_html(html)
    formats = rules.get("date_formats") or DEFAULT_DATE_FORMATS
    out = {}
    if rules.get("title_selector"):
        n = select_first(root, rules["title_selector"])
        if n is not None and n.inline_text():
            out["title"] = n.inline_text()
    if rules.get("date_selector"):
        d = node_date(select_first(root, rules["date_selector"]), formats)
        if d:
            out["published_on"] = d
    if rules.get("body_selector"):
        n = select_first(root, rules["body_selector"])
        if n is not None:
            body = n.text()
            # The title and date often sit inside the body container; keep the prose.
            lines = [ln for ln in body.splitlines() if ln != out.get("title")]
            body = "\n".join(lines).strip()
            if body:
                out["body"] = body
    return out


def missing_fields(rec: dict, required) -> list:
    return [f for f in required if not (isinstance(rec.get(f), str) and rec.get(f).strip())]


def agency_names_for(source: dict) -> list:
    wl_path = base.PACK_ROOT / "watchlist.yaml"
    try:
        wl = base.load_yaml(wl_path) if wl_path.is_file() else {}
    except (ValueError, OSError):
        wl = {}
    by_id = {a.get("id"): a for a in ((wl or {}).get("agencies") or []) if isinstance(a, dict)}
    return [(by_id.get(a) or {}).get("name") or a for a in source.get("agencies") or []]


def build_item(src: base.Source, raw: dict, required, default_type: str) -> dict:
    """The item shape for a newsroom record (kinds html_list and rss)."""
    if not isinstance(raw, dict):
        raise base.NormalizeError("record is not an object")
    miss = missing_fields(raw, required)
    if miss:
        raise base.NormalizeError(f"{raw.get('url')}: record is missing {', '.join(miss)}")
    rec = dict(raw)
    rec.setdefault("url_hash", url_hash(raw["url"]))
    c = src.config
    text = {}
    for f in c.get("text_fields") or ["title", "body"]:
        v = rec.get(f)
        if isinstance(v, str) and v.strip():
            text[f] = v
    return {
        "id": base.fill_template(c.get("item_id", src.id + "-{url_hash}"), rec),
        "source": src.id,
        "kind": src.kind,
        "doc_id": rec["url_hash"],
        "canonical_key": base.fill_template(c.get("canonical_key", "{url}"), rec),
        "title": rec.get("title"),
        "url": rec.get("url"),
        "type": rec.get("type") or default_type,
        "stage": c.get("stage", "announcement"),
        "published": rec.get("published_on"),
        "docket_ids": [],
        "agencies": agency_names_for(src.source),
        "dates": {"published_on": rec.get("published_on")},
        "text": text,
    }


# ---------------------------------------------------------------------------
# The connector


@base.register("html_list")
class HtmlListSource(base.Source):
    """Options: ``page_cap``, ``max_items`` (as every kind)."""

    @property
    def rules(self) -> dict:
        return self.source.get("extract") or {}

    @property
    def required(self) -> tuple:
        return tuple(self.rules.get("required_fields") or DEFAULT_REQUIRED)

    def _host_ok(self, url: str) -> bool:
        return (urllib.parse.urlsplit(url).hostname or "").lower() in self.hosts

    def list_entries(self, html: str, page_url: str, batch: base.Batch) -> list:
        """Entries on a list page: dicts with url, list_title, list_date (may be None)."""
        rules = self.rules.get("list") or {}
        if not rules.get("item_selector"):
            raise SelectorError("extract.list.item_selector is not set")
        formats = rules.get("date_formats") or DEFAULT_DATE_FORMATS
        root = parse_html(html)
        items = select(root, rules["item_selector"])
        if not items:
            raise base.SourceError(
                f"zero matches for extract.list.item_selector {rules['item_selector']!r} on {page_url} "
                "(the page layout may have changed)")
        out = []
        for i, it in enumerate(items, 1):
            link = select_first(it, rules.get("link_selector") or "a")
            href = (link.attrs.get(rules.get("link_attr") or "href") or "").strip() if link is not None else ""
            if not href:
                batch.errors.append(self.error("list", page_url, f"entry {i}: no link matched "
                                               f"{rules.get('link_selector') or 'a'!r}"))
                continue
            url = urllib.parse.urljoin(page_url, href).split("#", 1)[0]
            if rules.get("same_host_only", True) and not self._host_ok(url):
                batch.errors.append(self.error("list", url, f"entry {i}: link is not on a declared host"))
                continue
            title_node = select_first(it, rules["title_selector"]) if rules.get("title_selector") else link
            date_node = select_first(it, rules["date_selector"]) if rules.get("date_selector") else None
            out.append({
                "url": url,
                "list_title": title_node.inline_text() if title_node is not None else None,
                "list_date": node_date(date_node, formats),
            })
        if not out:
            raise base.SourceError(f"no usable entries among {len(items)} matches on {page_url}")
        return out

    def fetch(self, window: base.Window) -> base.Batch:
        batch = base.Batch()
        page_url = self.source["url"]
        try:
            html = self.get(page_url).text()
            entries = self.list_entries(html, page_url, batch)
        except base.SourceError as e:
            batch.errors.append(self.error("page", page_url, e))
            return batch
        dated = [e["list_date"] for e in entries if e["list_date"]]
        if dated and all(in_window(d, window) for d in dated):
            # The whole first page is in the window: there may be more on the next page.
            batch.truncated = True
        seen = set()
        art_rules = self.rules.get("article") or {}
        for e in entries:
            if e["url"] in seen:
                continue
            seen.add(e["url"])
            if e["list_date"] and not in_window(e["list_date"], window):
                continue
            if len(batch) >= self.max_items:
                batch.truncated = True
                break
            try:
                art = extract_article(self.get(e["url"]).text(), art_rules)
            except base.SourceError as err:
                batch.errors.append(self.error("article", e["url"], err))
                continue
            rec = {
                "url": e["url"],
                "title": art.get("title") or e["list_title"],
                "published_on": art.get("published_on") or e["list_date"],
                "body": art.get("body"),
                "list_title": e["list_title"],
                "list_date": e["list_date"],
                "type": "press release",
            }
            miss = missing_fields(rec, self.required)
            if miss:
                batch.errors.append(self.error("article", e["url"], f"malformed: missing {', '.join(miss)}"))
                continue
            if rec["published_on"] and not in_window(rec["published_on"], window):
                continue
            batch.append(rec)
        return batch

    def normalize_one(self, raw: dict) -> dict:
        return build_item(self, raw, self.required, "press release")
