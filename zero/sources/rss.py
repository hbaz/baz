"""Kind ``rss``: an agency feed in RSS 2.0 or Atom 1.0. Standard library only.

The feed URL is the source's ``url``. Each entry becomes one raw record with
``url`` (the entry link), ``title``, ``published_on`` (ISO date) and
``body``. The body is the entry's own content (``content:encoded``,
``description``, Atom ``content`` or ``summary``, reduced to text) unless the
source file has ``extract.article`` rules, in which case the linked article
page is fetched and read with them (the same rules and reader as kind
``html_list``).

Failures are recorded, never skipped silently:
  - a body that is not well-formed XML, holds a DOCTYPE with entity
    declarations (refused: entity expansion), or whose root is neither
    ``rss`` nor an Atom ``feed``: stage ``page``;
  - a feed with zero entries: stage ``page``, "zero matches";
  - an entry missing a required field (no link, title or parsable date,
    or no body): stage ``entry`` or ``article``, "malformed".

Feeds are one document; there is no paging here.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import base
from . import html_list

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"
DEFAULT_REQUIRED = ("title", "url", "published_on", "body")
_ENTITY_DECL = re.compile(r"<!DOCTYPE[^>]*\[.*?<!ENTITY", re.S | re.I)


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_feed(data: str) -> tuple:
    """(format, entries) where entries are dicts with url, title, date_text, content."""
    if _ENTITY_DECL.search(data):
        raise base.SourceError("feed declares XML entities; refused")
    try:
        root = ET.fromstring(data.encode("utf-8"))
    except ET.ParseError as e:
        raise base.SourceError(f"malformed feed XML: {e}") from e
    entries = []
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise base.SourceError("RSS document has no channel")
        for it in channel.findall("item"):
            guid = it.find("guid")
            permalink = _text(guid) if guid is not None and guid.get("isPermaLink", "true") != "false" else ""
            entries.append({
                "url": _text(it.find("link")) or permalink,
                "title": _text(it.find("title")),
                "date_text": _text(it.find("pubDate")) or _text(it.find(DC + "date")),
                "content": _text(it.find(CONTENT + "encoded")) or _text(it.find("description")),
            })
        return "rss", entries
    if root.tag == ATOM + "feed":
        for it in root.findall(ATOM + "entry"):
            href = ""
            for link in it.findall(ATOM + "link"):
                if link.get("rel", "alternate") == "alternate" and link.get("href"):
                    href = link.get("href")
                    break
            content = it.find(ATOM + "content")
            if content is None or not _text(content):
                content = it.find(ATOM + "summary")
            entries.append({
                "url": href,
                "title": _text(it.find(ATOM + "title")),
                "date_text": _text(it.find(ATOM + "published")) or _text(it.find(ATOM + "updated")),
                "content": _text(content),
            })
        return "atom", entries
    raise base.SourceError(f"not an RSS or Atom feed (root element {root.tag!r})")


@base.register("rss")
class RssSource(base.Source):
    """Options: ``max_items`` (as every kind)."""

    @property
    def rules(self) -> dict:
        return self.source.get("extract") or {}

    @property
    def required(self) -> tuple:
        return tuple(self.rules.get("required_fields") or DEFAULT_REQUIRED)

    def fetch(self, window: base.Window) -> base.Batch:
        batch = base.Batch()
        feed_url = self.source["url"]
        try:
            fmt, entries = parse_feed(self.get(feed_url).text())
            if not entries:
                raise base.SourceError(f"zero matches: the {fmt} feed at {feed_url} has no entries")
        except base.SourceError as e:
            batch.errors.append(self.error("page", feed_url, e))
            return batch
        formats = (self.rules.get("article") or {}).get("date_formats") or html_list.DEFAULT_DATE_FORMATS
        art_rules = self.rules.get("article")
        seen = set()
        for i, e in enumerate(entries, 1):
            url = urllib.parse.urljoin(feed_url, e["url"]).split("#", 1)[0] if e["url"] else ""
            published = html_list.parse_date(e["date_text"], formats)
            rec = {
                "url": url,
                "title": " ".join(e["title"].split()),
                "published_on": published,
                "body": base.html_to_text(e["content"]) if e["content"] else "",
                "type": "news release",
                "feed_format": fmt,
            }
            pre = html_list.missing_fields(rec, ("url", "title", "published_on"))
            if pre:
                batch.errors.append(self.error("entry", url or f"{feed_url}#entry-{i}",
                                               f"malformed: entry {i} is missing {', '.join(pre)}"))
                continue
            if url in seen or not html_list.in_window(published, window):
                continue
            seen.add(url)
            if art_rules and (urllib.parse.urlsplit(url).hostname or "").lower() not in self.hosts:
                batch.errors.append(self.error("entry", url, f"entry {i}: link is not on a declared host"))
                continue
            if len(batch) >= self.max_items:
                batch.truncated = True
                break
            if art_rules:
                try:
                    art = html_list.extract_article(self.get(url).text(), art_rules)
                except base.SourceError as err:
                    batch.errors.append(self.error("article", url, err))
                    continue
                rec["body"] = art.get("body") or ""
                rec["title"] = art.get("title") or rec["title"]
            miss = html_list.missing_fields(rec, self.required)
            if miss:
                stage = "article" if art_rules else "entry"
                batch.errors.append(self.error(stage, url, f"malformed: missing {', '.join(miss)}"))
                continue
            batch.append(rec)
        return batch

    def normalize_one(self, raw: dict) -> dict:
        return html_list.build_item(self, raw, self.required, "news release")
