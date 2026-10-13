"""Tests for zero/scan.py, run offline over the T207 fixtures. Standard library only."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ZERO = pathlib.Path(__file__).resolve().parent.parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

import scan  # noqa: E402
from sources import base  # noqa: E402

WINDOW = base.Window.parse("2026-10-07", "2026-10-09")
FEDREG_IDS = [
    "fedreg-2026-90101", "fedreg-2026-90102", "fedreg-2026-90103",
    "fedreg-2026-90201", "fedreg-2026-90202",
    "fedreg-2026-90301", "fedreg-2026-90302", "fedreg-2026-90303",
]


def sim_factory():
    fetcher = base.PoliteFetcher(transport=base.FixtureTransport(), sleep=lambda s: None)
    return lambda: fetcher


def offline_factory():
    """Every request fails like a network outage."""

    def transport(url, headers, timeout):
        raise base.SourceError(f"network down for {url}")

    fetcher = base.PoliteFetcher(transport=transport, sleep=lambda s: None, honor_robots=False)
    return lambda: fetcher


def run_main(*argv):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = scan.main(list(argv))
    return rc, err.getvalue()


def read_jsonl(path):
    return [json.loads(ln) for ln in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if ln]


class ScanTests(unittest.TestCase):
    def test_sim_scans_enabled_sources_and_dedupes_across_them(self):
        ids = scan.enabled_ids(scan.load_policies(scan.DEFAULT_POLICIES))
        summary, items = scan.scan(ids, WINDOW, sim_factory(), sim=True)
        self.assertEqual([e["id"] for e in summary["sources"]], ids)
        got = [i["id"] for i in items]
        self.assertEqual(len(got), len(set(got)), "an id appears twice")
        for fid in FEDREG_IDS:
            self.assertIn(fid, got)
        by_id = {e["id"]: e for e in summary["sources"]}
        self.assertEqual(by_id["fedreg"]["new_count"], 8)
        # the published fedreg record wins over the Public Inspection one with its id
        self.assertGreater(by_id["fedreg_pi"]["duplicate_count"], 0)
        self.assertEqual(next(i for i in items if i["id"] == "fedreg-2026-90101")["source"], "fedreg")
        for e in summary["sources"]:
            self.assertEqual(e["status"], "ok", e)
            self.assertEqual(e["item_count"], e["new_count"] + e["duplicate_count"] + e["known_count"])
        for i in items:
            for f in scan.REQUIRED_FIELDS:
                self.assertTrue(i.get(f), (i.get("id"), f))
            self.assertIn("scanned_at", i)

    def test_known_ids_are_not_new(self):
        known = {"fedreg-2026-90101", "fedreg-2026-90303"}
        summary, items = scan.scan(["fedreg"], WINDOW, sim_factory(), known=known)
        got = {i["id"] for i in items}
        self.assertFalse(got & known)
        self.assertEqual(len(got), 6)
        self.assertEqual(summary["sources"][0]["known_count"], 2)

    def test_failed_source_is_recorded_not_skipped(self):
        summary, items = scan.scan(["fedreg", "no_such_source"], WINDOW, offline_factory())
        self.assertEqual(items, [])
        self.assertEqual([e["id"] for e in summary["sources"]], ["fedreg", "no_such_source"])
        for e in summary["sources"]:
            self.assertEqual(e["status"], "failed")
            self.assertTrue(e["errors"], e)
        self.assertEqual(summary["failed_sources"], ["fedreg", "no_such_source"])
        self.assertEqual(summary["sources"][1]["errors"][0]["stage"], "load")

    def test_failed_source_does_not_stop_the_others(self):
        summary, items = scan.scan(["no_such_source", "fedreg"], WINDOW, sim_factory())
        self.assertEqual(summary["sources"][0]["status"], "failed")
        self.assertEqual(summary["sources"][1]["status"], "ok")
        self.assertEqual(len(items), 8)

    def test_bad_sources_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "private.yaml").write_text(
                "id: private\nkind: fedreg\nurl: \"https://www.federalregister.gov/x\"\n"
                "hosts: [\"www.federalregister.gov\"]\ndata_class: restricted\n", encoding="utf-8")
            (d / "oddkind.yaml").write_text(
                "id: oddkind\nkind: nosuchkind\nurl: \"https://example.invalid/\"\n"
                "hosts: [\"example.invalid\"]\ndata_class: public\n", encoding="utf-8")
            (d / "off.yaml").write_text(
                "id: off\nkind: html_list\nenabled: false\nstatus: blocked_by_site\n"
                "data_class: public\n", encoding="utf-8")
            (d / "wrongid.yaml").write_text("id: other\nkind: fedreg\n", encoding="utf-8")
            summary, items = scan.scan(["private", "oddkind", "off", "wrongid"], WINDOW,
                                       sim_factory(), sources_dir=d)
        st = {e["id"]: e for e in summary["sources"]}
        self.assertEqual(st["private"]["status"], "failed")
        self.assertIn("public", st["private"]["errors"][0]["error"])
        self.assertEqual(st["oddkind"]["status"], "failed")
        self.assertEqual(st["wrongid"]["status"], "failed")
        self.assertEqual(st["off"]["status"], "disabled")
        self.assertIn("blocked_by_site", st["off"]["reason"])
        self.assertEqual(items, [])

    def test_item_missing_core_field_is_recorded(self):
        class Bad(base.Source):
            def fetch(self, window):
                return base.Batch([{"n": 1}, {"n": 2}])

            def normalize_one(self, raw):
                return {"id": f"bad-{raw['n']}", "source": self.id, "kind": self.kind,
                        "title": "t" if raw["n"] == 1 else ""}

        base.REGISTRY["_scan_test_bad"] = Bad
        Bad.kind = "_scan_test_bad"
        self.addCleanup(base.REGISTRY.pop, "_scan_test_bad", None)
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "bad.yaml").write_text(
                "id: bad\nkind: _scan_test_bad\nhosts: [\"example.invalid\"]\ndata_class: public\n",
                encoding="utf-8")
            summary, items = scan.scan(["bad"], WINDOW, sim_factory(), sources_dir=d)
        e = summary["sources"][0]
        self.assertEqual([i["id"] for i in items], ["bad-1"])
        self.assertEqual(e["invalid_count"], 1)
        self.assertEqual(e["status"], "partial")
        self.assertEqual(e["errors"][0]["stage"], "schema")

    def test_load_known_ids_formats(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "a.json").write_text('["x", "y"]', encoding="utf-8")
            (d / "b.json").write_text('{"ids": ["z"]}', encoding="utf-8")
            (d / "c.txt").write_text("# known\nx\n\n y \n", encoding="utf-8")
            (d / "bad.json").write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(scan.load_known_ids(d / "a.json"), {"x", "y"})
            self.assertEqual(scan.load_known_ids(d / "b.json"), {"z"})
            self.assertEqual(scan.load_known_ids(d / "c.txt"), {"x", "y"})
            with self.assertRaises(scan.UsageError):
                scan.load_known_ids(d / "bad.json")
            with self.assertRaises(scan.UsageError):
                scan.load_known_ids(d / "missing.json")


class CliTests(unittest.TestCase):
    def test_sim_writes_items_and_summary_to_out(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "o"
            rc, err = run_main("--sim", "--out", str(out))
            self.assertEqual(rc, 0, err)
            items = read_jsonl(out / scan.ITEMS_FILE)
            summary = json.loads((out / scan.SUMMARY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(summary["mode"], "sim")
        # without --today the simulator ends the window on the latest fixture day
        self.assertEqual(summary["window"], {"start": "2026-10-07", "end": "2026-10-09", "days": 3})
        self.assertEqual(summary["new_items"], len(items))
        self.assertEqual(sorted(i["id"] for i in items), sorted({i["id"] for i in items}))
        self.assertTrue(set(FEDREG_IDS) <= {i["id"] for i in items})

    def test_known_file_and_rerun_gives_nothing_new(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            rc, _ = run_main("--sim", "--out", str(d / "1"))
            self.assertEqual(rc, 0)
            ids = [i["id"] for i in read_jsonl(d / "1" / scan.ITEMS_FILE)]
            (d / "known.json").write_text(json.dumps(ids), encoding="utf-8")
            rc, _ = run_main("--sim", "--out", str(d / "2"), "--known", str(d / "known.json"))
            self.assertEqual(rc, 0)
            self.assertEqual(read_jsonl(d / "2" / scan.ITEMS_FILE), [])
            summary = json.loads((d / "2" / scan.SUMMARY_FILE).read_text(encoding="utf-8"))
            self.assertEqual(summary["known_ids"], len(ids))

    def test_failed_source_exits_1_with_outputs_written(self):
        with tempfile.TemporaryDirectory() as d:
            rc, err = run_main("--sim", "--out", d, "--sources", "fedreg,no_such_source")
            self.assertEqual(rc, 1)
            self.assertIn("no_such_source: failed", err)
            summary = json.loads(pathlib.Path(d, scan.SUMMARY_FILE).read_text(encoding="utf-8"))
            self.assertEqual(summary["failed_sources"], ["no_such_source"])
            self.assertEqual(len(read_jsonl(pathlib.Path(d, scan.ITEMS_FILE))), 8)

    def test_window_outside_fixtures_is_a_recorded_failure(self):
        with tempfile.TemporaryDirectory() as d:
            rc, err = run_main("--sim", "--out", d, "--sources", "fedreg",
                               "--today", "2026-09-01", "--lookback-days", "1")
            self.assertEqual(rc, 1, err)  # a 404 page is a recorded failure
            summary = json.loads(pathlib.Path(d, scan.SUMMARY_FILE).read_text(encoding="utf-8"))
            self.assertEqual(summary["window"]["start"], "2026-09-01")
            self.assertEqual(summary["sources"][0]["status"], "failed")

    def test_usage_errors_exit_2_and_write_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _ = run_main("--sim", "--out", d, "--today", "not-a-date")
            self.assertEqual(rc, 2)
            rc, _ = run_main("--sim", "--out", d, "--known", str(pathlib.Path(d, "missing")))
            self.assertEqual(rc, 2)
            rc, _ = run_main("--sim", "--out", d, "--policies", str(pathlib.Path(d, "missing.yaml")))
            self.assertEqual(rc, 2)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
