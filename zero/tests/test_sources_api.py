"""Tests for the source plug-in interface (zero/sources/base.py) and the
federal API connectors (fedreg, fedreg_pi), run offline over the T207
synthesized fixtures in zero/fixtures/. Standard library only."""

from __future__ import annotations

import pathlib
import sys
import unittest
import urllib.parse

ZERO = pathlib.Path(__file__).resolve().parent.parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402
from sources import fedreg, fedreg_pi  # noqa: E402,F401  (registers the kinds)

WINDOW = base.Window.parse("2026-10-07", "2026-10-09")
FEDREG_IDS = [
    "fedreg-2026-90101", "fedreg-2026-90102", "fedreg-2026-90103",
    "fedreg-2026-90201", "fedreg-2026-90202",
    "fedreg-2026-90301", "fedreg-2026-90302", "fedreg-2026-90303",
]


def load_source(sid: str) -> dict:
    return base.load_source_file(ZERO / "pack" / "sources" / f"{sid}.yaml")


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Recorder:
    """Wraps a transport and records each request's URL and headers."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        return self.inner(url, headers, timeout)


def robots_transport(robots_body: str, inner=None):
    inner = inner or base.FixtureTransport()

    def t(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return base.Response(url, 200, robots_body.encode())
        return inner(url, headers, timeout)

    return t


def make(sid, transport=None, **options):
    clk = FakeClock()
    rec = Recorder(transport or base.FixtureTransport())
    fetcher = base.PoliteFetcher(transport=rec, clock=clk.clock, sleep=clk.sleep)
    return base.create(load_source(sid), fetcher=fetcher, **options), rec, clk


class RegistryTests(unittest.TestCase):
    def test_kinds_registered_by_name(self):
        self.assertIs(base.get_kind("fedreg"), fedreg.FedRegSource)
        self.assertIs(base.get_kind("fedreg_pi"), fedreg_pi.FedRegPISource)
        self.assertEqual(fedreg.FedRegSource.kind, "fedreg")

    def test_unknown_and_invalid_kinds_refused(self):
        with self.assertRaises(KeyError):
            base.get_kind("no_such_kind_zz")
        with self.assertRaises(KeyError):
            base.get_kind("../etc")

    def test_duplicate_registration_refused(self):
        with self.assertRaises(ValueError):
            base.register("fedreg")(type("Other", (base.Source,), {}))

    def test_non_public_or_hostless_source_refused(self):
        src = dict(load_source("fedreg"), data_class="internal")
        with self.assertRaises(ValueError):
            base.create(src, fetcher=base.fixture_fetcher())
        src = dict(load_source("fedreg"), hosts=[])
        with self.assertRaises(ValueError):
            base.create(src, fetcher=base.fixture_fetcher())


class PoliteFetcherTests(unittest.TestCase):
    def test_user_agent_robots_once_and_host_delay(self):
        s, rec, clk = make("fedreg")
        res = s.run(WINDOW)
        self.assertTrue(res.ok, res.errors)
        urls = [u for u, _ in rec.calls]
        self.assertEqual(sum(u.endswith("/robots.txt") for u in urls), 1)
        for _, h in rec.calls:
            self.assertTrue(h["User-Agent"].startswith(f"{base.PRODUCT}/{base.VERSION}"))
            self.assertIn("contact", h["User-Agent"])
        # robots + 5 pages = 6 requests, each after the first spaced by 1 s.
        self.assertEqual(len(rec.calls), 6)
        self.assertEqual(len(clk.slept), 5)
        for s_ in clk.slept:
            self.assertAlmostEqual(s_, 1.0)

    def test_delay_clamped(self):
        self.assertEqual(base.clamp_delay_ms(0), base.HOST_DELAY_FLOOR_MS)
        self.assertEqual(base.clamp_delay_ms(10 ** 9), base.HOST_DELAY_CEILING_MS)
        self.assertEqual(base.clamp_delay_ms("x"), base.HOST_DELAY_DEFAULT_MS)

    def test_robots_disallow_is_a_recorded_failure(self):
        t = robots_transport("User-agent: *\nDisallow: /api/\n")
        s, rec, _ = make("fedreg", transport=t)
        res = s.run(WINDOW)
        self.assertEqual(res.items, [])
        self.assertEqual(len(res.errors), 3)  # one per day, none dropped
        self.assertTrue(all("robots.txt disallows" in e["error"] for e in res.errors))
        self.assertEqual(len(rec.calls), 1)  # only robots.txt was requested

    def test_undeclared_host_refused(self):
        f = base.fixture_fetcher()
        with self.assertRaises(base.HostNotAllowed):
            f.get("https://example.org/x", hosts=["www.federalregister.gov"])
        with self.assertRaises(base.SourceError):
            f.get("file:///etc/passwd")


class FedRegTests(unittest.TestCase):
    def test_pages_and_normalizes_fixtures(self):
        s, _, _ = make("fedreg")
        res = s.run(WINDOW)
        self.assertTrue(res.ok, res.errors)
        self.assertFalse(res.truncated)
        self.assertEqual([i["id"] for i in res.items], FEDREG_IDS)
        first = res.items[0]
        self.assertEqual(first["source"], "fedreg")
        self.assertEqual(first["kind"], "fedreg")
        self.assertEqual(first["stage"], "proposed")
        self.assertEqual(first["published"], "2026-10-07")
        self.assertIn("Transportation Department", first["agencies"])
        self.assertIn("title", first["text"])
        self.assertTrue(first["url"].startswith("https://www.federalregister.gov/"))

    def test_page_cap_sets_truncated(self):
        s, _, _ = make("fedreg", page_cap=1)
        res = s.run(WINDOW)
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.items), 6)  # page 2 of 10-07 and 10-09 not read
        self.assertTrue(res.ok)

    def test_max_items_sets_truncated(self):
        s, _, _ = make("fedreg", max_items=4)
        res = s.run(WINDOW)
        self.assertTrue(res.truncated)
        self.assertEqual([i["id"] for i in res.items], FEDREG_IDS[:4])

    def test_failed_page_recorded_other_days_kept(self):
        s, _, _ = make("fedreg")
        res = s.run(base.Window.parse("2026-10-08", "2026-10-10"))  # no fixture for 10-10
        self.assertEqual(len(res.items), 5)
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.errors[0]["stage"], "page")
        self.assertIn("HTTP 404", res.errors[0]["error"])

    def test_bad_record_recorded_not_dropped(self):
        s, _, _ = make("fedreg")
        with self.assertRaises(base.NormalizeError):
            s.normalize([{"title": "no number"}])

        class Broken(fedreg.FedRegSource):
            def fetch(self, window):
                b = base.Batch([{"document_number": "X-1", "title": "ok"}, {"title": "no number"}])
                return b

        b = Broken(load_source("fedreg"), fetcher=base.fixture_fetcher())
        res = b.run(WINDOW)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.errors[0]["stage"], "normalize")


class FedRegAgencyFilterTests(unittest.TestCase):
    # Spike S2: one agency-filtered fedreg source per DOT agency.
    EXPECTED = {
        "fedreg_dot": ("transportation-department", False),
        "fedreg_nhtsa": ("national-highway-traffic-safety-administration", True),
        "fedreg_fmcsa": ("federal-motor-carrier-safety-administration", False),
    }
    TYPES = ["RULE", "PRORULE", "NOTICE"]

    def test_fedreg_agency_filter(self):
        enabled = base.load_yaml(ZERO / "policies.yaml")["sources"]["enabled"]
        for sid, (slug, verified) in self.EXPECTED.items():
            with self.subTest(source=sid):
                src = load_source(sid)
                self.assertEqual(src["kind"], "fedreg")
                self.assertEqual(src["data_class"], "public")
                self.assertIs(src["names_people"], True)
                self.assertEqual(src["agency_slug"], slug)
                self.assertIs(src["slug_verified"], verified)
                self.assertIn(sid, enabled)
                # Recorded fixtures only: FixtureTransport never opens a socket.
                transport = base.FixtureTransport()
                s, rec, _ = make(sid, transport=transport)
                res = s.run(WINDOW)
                self.assertTrue(res.ok, res.errors)
                self.assertTrue(res.items)
                pages = [u for u, _ in rec.calls if not u.endswith("/robots.txt")]
                self.assertEqual(len(pages), 5)
                self.assertEqual(len(transport.served), 5)  # every page answered by a fixture
                # The connector builds the first page of each day's query; later
                # pages follow the response's next_page_url (from the fixture).
                built = [s.first_page_url(d) for d in WINDOW.days()]
                self.assertEqual([u for u in pages if u in built], built)
                for url in built:
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    self.assertEqual(q["conditions[agencies][]"], [slug])
                    self.assertEqual(q["conditions[type][]"], self.TYPES)


class FedRegPITests(unittest.TestCase):
    def test_pages_and_normalizes_fixtures(self):
        s, _, _ = make("fedreg_pi")
        res = s.run(WINDOW)
        self.assertTrue(res.ok, res.errors)
        self.assertFalse(res.truncated)
        self.assertEqual(len(res.items), 6)
        for it in res.items:
            self.assertEqual(it["stage"], "pre_publication")
            self.assertIsNone(it["published"])
            self.assertTrue(it["title"])
        # A PI item shares its id and canonical key with the later fedreg record.
        pub, _, _ = make("fedreg")
        pub_ids = {i["id"] for i in pub.run(WINDOW).items}
        self.assertIn("fedreg-2026-90201", pub_ids & {i["id"] for i in res.items})

    def test_cap_and_failure(self):
        s, _, _ = make("fedreg_pi", max_items=2)
        res = s.run(WINDOW)
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.items), 2)
        s, _, _ = make("fedreg_pi")
        res = s.run(base.Window.parse("2026-10-09", "2026-10-10"))
        self.assertEqual(len(res.items), 1)
        self.assertEqual(len(res.errors), 1)


if __name__ == "__main__":
    unittest.main()
