"""Tests for the agency-website connectors, kinds ``html_list`` and ``rss``,
run offline over the synthesized newsroom fixtures in zero/fixtures/newsrooms/
(real page shape, invented officials' names). Standard library only."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ZERO = pathlib.Path(__file__).resolve().parent.parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402
from sources import html_list, rss  # noqa: E402  (registers the kinds)

NEWSROOMS = ZERO / "fixtures" / "newsrooms"
WINDOW = base.Window.parse("2026-10-07", "2026-10-09")
EX = "https://newsroom.example.org"


def load_source(sid: str) -> dict:
    return base.load_source_file(ZERO / "pack" / "sources" / f"{sid}.yaml")


def feed_source(path: str, **extra) -> dict:
    src = {"id": "example_feed", "kind": "rss", "url": EX + path, "hosts": ["newsroom.example.org"],
           "data_class": "public", "names_people": True,
           "config": {"item_id": "example_feed-{url_hash}", "canonical_key": "{url}", "stage": "announcement",
                      "text_fields": ["title", "body"]}}
    src.update(extra)
    return src


def run(src: dict, transport=None, window=WINDOW, **options) -> base.RunResult:
    fetcher = base.PoliteFetcher(transport=transport or base.FixtureTransport(NEWSROOMS), sleep=lambda s: None)
    return base.create(src, fetcher=fetcher, **options).run(window)


def pages(mapping: dict, fallback=None):
    """A transport answering from {url: body}; robots.txt and anything else 404."""
    def t(url, headers, timeout):
        if url in mapping:
            return base.Response(url, 200, mapping[url].encode("utf-8"))
        if fallback is not None:
            return fallback(url, headers, timeout)
        return base.Response(url, 404, b"")
    return t


def list_page(rows) -> str:
    out = "".join(f'<div class="views-row"><h2><a href="{h}">{t}</a></h2><time datetime="{d}">{d}</time></div>'
                  for h, t, d in rows)
    return f'<html><body><main><div class="view-content">{out}</div></main></body></html>'


def article(title, date, body) -> str:
    return (f'<html><body><main><article><h1>{title}</h1><div class="date"><time datetime="{date}">x</time></div>'
            f'<div class="field--name-body"><p>{body}</p></div></article></main></body></html>')


class HtmlListFixtureTests(unittest.TestCase):
    def test_usdot_list_and_articles(self):
        r = run(load_source("usdot_news"))
        self.assertEqual(r.errors, [])
        self.assertEqual(r.raw_count, 3)  # the 2026-10-01 entry is outside the window
        self.assertFalse(r.truncated)
        self.assertEqual([i["published"] for i in r.items], ["2026-10-09", "2026-10-08", "2026-10-07"])
        first = r.items[0]
        self.assertEqual(first["kind"], "html_list")
        self.assertEqual(first["source"], "usdot_news")
        self.assertEqual(first["id"], "usdot_news-" + html_list.url_hash(first["url"]))
        self.assertEqual(first["canonical_key"], first["url"])
        self.assertTrue(first["url"].startswith("https://www.transportation.gov/briefing-room/"))
        self.assertEqual(first["stage"], "announcement")
        self.assertEqual(first["agencies"], ["U.S. Department of Transportation (Office of the Secretary)"])
        self.assertIn("Secretary Marisol Quenby", first["text"]["body"])
        self.assertNotIn("Skip to main content", first["text"]["body"])
        self.assertNotIn("dataLayer", first["text"]["body"])
        self.assertEqual(r.items[1]["title"],
                         "USDOT Issues Guidance on Mobile Driver's License Acceptance at Transportation Facilities")

    def test_malformed_article_is_a_recorded_failure(self):
        r = run(load_source("nhtsa_news"))
        self.assertEqual(len(r.items), 1)
        self.assertEqual(len(r.errors), 1, r.errors)
        e = r.errors[0]
        self.assertEqual((e["source"], e["stage"]), ("nhtsa_news", "article"))
        self.assertIn("malformed", e["error"])
        self.assertIn("body", e["error"])
        self.assertTrue(e["url"].endswith("/nhtsa-statement-state-crash-data-reporting"))
        self.assertFalse(r.ok)

    def test_changed_layout_is_zero_matches_failure(self):
        r = run(load_source("fmcsa_news"))
        self.assertEqual(r.items, [])
        self.assertFalse(r.ok)
        self.assertEqual(len(r.errors), 1, r.errors)
        e = r.errors[0]
        self.assertEqual(e["stage"], "page")
        self.assertIn("zero matches", e["error"])
        self.assertIn(".view-content .views-row", e["error"])

    def test_updated_rules_read_the_changed_layout(self):
        src = load_source("fmcsa_news")
        src["extract"]["list"].update({"item_selector": "ul.usa-collection > li.usa-collection__item",
                                       "title_selector": ".usa-collection__heading a",
                                       "date_selector": "time"})
        r = run(src)
        self.assertEqual(r.errors, [])
        self.assertTrue(r.truncated)  # every entry on the first page is in the window
        self.assertEqual([i["published"] for i in r.items], ["2026-10-08", "2026-10-07"])
        self.assertIn("Administrator Lucinda Farrowgate", r.items[0]["text"]["body"])
        self.assertEqual(r.items[0]["title"], "FMCSA Extends Comment Period on CDL Skills Testing Proposal")

    def test_fixtures_are_synthetic_and_include_a_changed_layout_page(self):
        files = sorted(NEWSROOMS.glob("*.json"))
        self.assertGreaterEqual(len(files), 10)
        changed = 0
        for p in files:
            doc = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(doc.get("provenance"), "synthetic", p.name)
            self.assertIsInstance(doc.get("response"), str, p.name)
            changed += "changed layout" in (doc.get("case") or "")
        self.assertGreaterEqual(changed, 1)


class HtmlListBehaviourTests(unittest.TestCase):
    URL = "https://www.nhtsa.gov/press-releases"

    def src(self, **list_rules):
        s = load_source("nhtsa_news")
        s["extract"]["list"].update(list_rules)
        return s

    def test_off_host_link_recorded_and_relative_links_resolved(self):
        t = pages({
            self.URL: list_page([("https://elsewhere.example.com/a", "Off host", "2026-10-08"),
                                 ("/press-releases/b", "On host", "2026-10-08"),
                                 ("/press-releases/old", "Old", "2026-09-01")]),
            "https://www.nhtsa.gov/press-releases/b": article("On host", "2026-10-08", "Text naming Jo Invented."),
        })
        r = run(self.src(), transport=t)
        self.assertEqual([i["url"] for i in r.items], ["https://www.nhtsa.gov/press-releases/b"])
        self.assertEqual(len(r.errors), 1)
        self.assertEqual(r.errors[0]["stage"], "list")
        self.assertIn("declared host", r.errors[0]["error"])

    def test_item_cap_and_full_page_set_truncated(self):
        rows = [(f"/press-releases/n{i}", f"N{i}", "2026-10-08") for i in range(4)]
        m = {self.URL: list_page(rows)}
        for h, title, d in rows:
            m["https://www.nhtsa.gov" + h] = article(title, d, "Body.")
        r = run(self.src(), transport=pages(m), max_items=2)
        self.assertEqual(len(r.items), 2)
        self.assertTrue(r.truncated)
        self.assertEqual(r.errors, [])

    def test_failed_article_page_and_bad_rule_are_recorded(self):
        t = pages({self.URL: list_page([("/press-releases/gone", "Gone", "2026-10-08")])})
        r = run(self.src(), transport=t)
        self.assertEqual(r.items, [])
        self.assertEqual([e["stage"] for e in r.errors], ["article"])
        self.assertIn("HTTP 404", r.errors[0]["error"])

        r = run(self.src(item_selector="div:nth-child(2)"), transport=t)
        self.assertEqual([e["stage"] for e in r.errors], ["page"])
        self.assertIn("unsupported selector", r.errors[0]["error"])

    def test_non_html_and_failed_list_page(self):
        r = run(self.src(), transport=pages({self.URL: '{"not": "html"}'}))
        self.assertIn("zero matches", r.errors[0]["error"])
        r = run(self.src(), transport=pages({}))
        self.assertEqual(r.errors[0]["stage"], "page")
        self.assertIn("HTTP 404", r.errors[0]["error"])

    def test_robots_disallow_is_recorded(self):
        def t(url, headers, timeout):
            if url.endswith("/robots.txt"):
                return base.Response(url, 200, b"User-agent: *\nDisallow: /press-releases\n")
            raise AssertionError(f"fetched a disallowed URL {url}")
        r = run(self.src(), transport=t)
        self.assertEqual(r.items, [])
        self.assertIn("robots.txt disallows", r.errors[0]["error"])


class SelectorTests(unittest.TestCase):
    HTML = """<div id="x" class="a b"><ul class="list"><li class="item">one<li class="item"><span data-k="v">two</span>
              </ul><p>para<div class="c">after p</div></div><section><p class="item">three</p></section>"""

    def setUp(self):
        self.root = html_list.parse_html(self.HTML)

    def texts(self, sel):
        return [n.inline_text() for n in html_list.select(self.root, sel)]

    def test_compound_descendant_child_and_alternatives(self):
        self.assertEqual(self.texts("li.item"), ["one", "two"])  # implied </li>
        self.assertEqual(self.texts(".item"), ["one", "two", "three"])
        self.assertEqual(self.texts("#x.a.b > ul > li"), ["one", "two"])
        self.assertEqual(self.texts("div > li"), [])
        self.assertEqual(self.texts("section .item, ul li"), ["one", "two", "three"])
        self.assertEqual(self.texts("[data-k=v]"), ["two"])
        self.assertEqual(self.texts("span[data-k]"), ["two"])
        self.assertEqual(self.texts("#x > .c"), ["after p"])  # a div closes the open <p>
        self.assertEqual(html_list.select_first(self.root, "nothing, section p").inline_text(), "three")

    def test_unsupported_selectors_raise(self):
        for bad in ("", "a:hover", "a + b", "> a", "a >", "a,,b", "a ~ b"):
            with self.assertRaises(html_list.SelectorError, msg=bad):
                html_list.parse_selector(bad)

    def test_dates(self):
        f = html_list.DEFAULT_DATE_FORMATS
        self.assertEqual(html_list.parse_date("October 8, 2026", f), "2026-10-08")
        self.assertEqual(html_list.parse_date("Oct. 8, 2026", f), "2026-10-08")
        self.assertEqual(html_list.parse_date("Posted Thursday, October 8, 2026", f), "2026-10-08")
        self.assertEqual(html_list.parse_date("2026-10-08T12:00:00Z", f), "2026-10-08")
        self.assertEqual(html_list.parse_date("10/08/2026", f), "2026-10-08")
        self.assertEqual(html_list.parse_date("Thu, 08 Oct 2026 10:00:00 -0400", f), "2026-10-08")
        self.assertIsNone(html_list.parse_date("soon", f))
        self.assertIsNone(html_list.parse_date("2026-13-40", f))


class RssTests(unittest.TestCase):
    def test_registry(self):
        self.assertIs(base.get_kind("rss"), rss.RssSource)
        self.assertIs(base.get_kind("html_list"), html_list.HtmlListSource)

    def test_rss2_feed(self):
        r = run(feed_source("/feeds/releases.rss"))
        self.assertEqual([i["published"] for i in r.items], ["2026-10-09", "2026-10-07"])
        self.assertEqual(r.items[0]["kind"], "rss")
        self.assertEqual(r.items[0]["id"], "example_feed-" + html_list.url_hash(r.items[0]["url"]))
        self.assertIn("Director Ottoline Brackenridge", r.items[0]["text"]["body"])
        self.assertNotIn("<p>", r.items[0]["text"]["body"])
        # content:encoded wins over the short description
        self.assertIn("Deputy Director Cassius Wenlock", r.items[1]["text"]["body"])
        # the undated entry is a recorded failure; the September entry is outside the window
        self.assertEqual(len(r.errors), 1, r.errors)
        self.assertEqual(r.errors[0]["stage"], "entry")
        self.assertIn("malformed", r.errors[0]["error"])
        self.assertIn("published_on", r.errors[0]["error"])

    def test_atom_feed(self):
        r = run(feed_source("/feeds/announcements.atom"))
        self.assertEqual(r.errors, [])
        self.assertEqual([(i["published"], i["url"].rsplit("/", 1)[1]) for i in r.items],
                         [("2026-10-08", "chief-of-driver-programs"), ("2026-10-09", "real-id-outreach-toolkit")])
        self.assertIn("Philippa Coldharbour", r.items[0]["text"]["body"])

    def test_feed_with_article_rules(self):
        rules = load_source("usdot_news")["extract"]
        src = feed_source("/feeds/releases.rss", extract={"article": rules["article"]})
        r = run(src)
        self.assertEqual(len(r.items), 2)
        self.assertIn("Comments are due December 8, 2026.", r.items[0]["text"]["body"])

    def test_broken_feeds_are_recorded_failures(self):
        url = EX + "/feeds/x.xml"
        cases = {
            "<rss><channel><item><title>unclosed</channel></rss>": "malformed feed XML",
            "<html><body>not a feed</body></html>": "not an RSS or Atom feed",
            '<rss version="2.0"><channel><title>t</title></channel></rss>': "zero matches",
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>': "zero matches",
            ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
             "<rss><channel><item><title>&b;</title></item></channel></rss>"): "entities",
        }
        for body, expect in cases.items():
            r = run(feed_source("/feeds/x.xml"), transport=pages({url: body}))
            self.assertEqual(r.items, [], body)
            self.assertEqual(len(r.errors), 1, body)
            self.assertEqual(r.errors[0]["stage"], "page", body)
            self.assertIn(expect, r.errors[0]["error"], body)

    def test_requests_are_polite_and_on_declared_hosts(self):
        fetcher = base.PoliteFetcher(transport=base.FixtureTransport(NEWSROOMS), sleep=lambda s: None)
        base.create(load_source("usdot_news"), fetcher=fetcher).run(WINDOW)
        hosts = {h for h, _, _ in fetcher.requests}
        self.assertEqual(hosts, {"www.transportation.gov"})
        self.assertTrue(any(u.endswith("/robots.txt") for _, u, _ in fetcher.requests))
        self.assertEqual(fetcher.delay_for("www.transportation.gov"), 2000)


if __name__ == "__main__":
    unittest.main()
