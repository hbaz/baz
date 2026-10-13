"""Tests for zero/validate.py, offline over the T205 pack and T207 fixtures. Standard library only."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest

ZERO = pathlib.Path(__file__).resolve().parent.parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

import validate  # noqa: E402

SAMPLE = ZERO / "fixtures" / "classified.sample.jsonl"

# The T207 sample was written with older value ids than the T205 pack holds.
# The tests translate them to the pack's ids so each sample record's _expect
# can be checked on its own; test_sample_as_committed_* covers the file as is.
LEGACY_IDS = {
    "policy_area": {
        "commercial_driver_licensing": "commercial_driver",
        "identity_and_credentials": "identity_credentials",
        "vehicle_registration_and_titling": "vehicle_titling_registration",
        "motor_carrier_operations": "motor_carrier",
    },
    "stage": {"proposed_rule": "proposed", "final_rule": "final"},
}


def load_sample(translate=True):
    recs = [json.loads(ln) for ln in SAMPLE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if translate:
        for r in recs:
            for key, m in LEGACY_IDS.items():
                r[key] = m.get(r[key], r[key])
    return recs


def raw_source(**over):
    raw = {
        "document_number": "2026-99001",
        "title": "Example Standards; Synthetic Test Document",
        "abstract": "The agency proposes that States would verify  each applicant's identity\nbefore issuing a learner's permit.",
        "action": "Notice of proposed rulemaking.",
        "publication_date": "2026-10-09",
        "comments_close_on": "2026-12-08",
        "effective_on": None,
        "docket_ids": ["EXAMPLE-2026-0001"],
    }
    raw.update(over)
    return raw


def good_record(**over):
    rec = {
        "id": "fedreg-2026-99001",
        "source": "fedreg",
        "doc_id": "2026-99001",
        "title": "Example Standards; Synthetic Test Document",
        "published": "2026-10-09",
        "comments_close_on": "2026-12-08",
        "effective_on": None,
        "docket_ids": ["EXAMPLE-2026-0001"],
        "policy_area": "driver_licensing",
        "impact": "direct",
        "stage": "proposed",
        "reason": "Proposes an identity check States would run before issuing a permit.",
        "signals": ["state_agency_named"],
        "committees": ["driver_committee"],
        "flags": [],
        "citations": [
            {"field": "abstract", "text": "States would verify each applicant's identity", "supports": "impact"},
            {"field": "abstract", "quote": "States would verify each applicant's identity before issuing",
             "supports": "state_agency_named"},
        ],
        "assessment": "States would verify identity before issuing a learner's permit.",
        "status": "published",
        "source_record": raw_source(),
    }
    rec.update(over)
    return rec


class ValidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = validate.Pack(validate.DEFAULT_PACK)

    def run_one(self, rec, items=None):
        return validate.validate_record(rec, self.pack, validate.SourceIndex(items or {}))

    def reasons(self, rec, items=None):
        return self.run_one(rec, items)["validation"]["reasons"]

    # -- the pack
    def test_pack_values_come_from_the_pack_files(self):
        self.assertIn("commercial_driver", self.pack.policy_areas)
        self.assertIn("other", self.pack.policy_areas)  # the fallback
        self.assertEqual(self.pack.impacts[0], "direct")
        self.assertIn("proposed", self.pack.stages)
        self.assertIn("direct", self.pack.requires_assessment)
        self.assertIn("comment_period_open", self.pack.signals)
        self.assertIn("driver_committee", self.pack.committees)

    # -- a good record
    def test_good_record_is_valid_and_status_is_set_by_code(self):
        out = self.run_one(good_record())
        self.assertEqual(out["validation"]["problems"], [])
        self.assertEqual(out["status"], "draft", "the model's 'published' must not pass through")
        self.assertNotIn("review_reason", out)

    def test_whitespace_runs_count_as_one_space_but_nothing_else_is_forgiven(self):
        self.assertEqual(self.reasons(good_record()), [])  # source has a double space and a newline
        rec = good_record()
        rec["citations"][0]["text"] = "states would verify each applicant's identity"
        self.assertIn("citation_not_verbatim", self.reasons(rec))

    # -- schema and enums
    def test_wrong_enums_need_review(self):
        for key, bad in (("policy_area", "commercial_driver_licensing"), ("impact", "critical"),
                         ("stage", "final_rule"), ("signals", ["made_up"]), ("committees", ["nobody"]),
                         ("flags", ["something"])):
            with self.subTest(key=key):
                self.assertIn("enum_not_in_pack", self.reasons(good_record(**{key: bad})))

    def test_missing_fields_and_bad_types_are_schema_invalid(self):
        for key in ("policy_area", "impact", "stage", "reason", "id"):
            with self.subTest(key=key):
                rec = good_record()
                del rec[key]
                self.assertIn("schema_invalid", self.reasons(rec))
        self.assertIn("schema_invalid", self.reasons(good_record(signals="state_agency_named")))
        self.assertIn("schema_invalid", self.reasons(good_record(citations=[{"field": "abstract"}])))
        self.assertIn("schema_invalid", self.reasons(good_record(reason="word " * 31)))

    def test_wrong_typed_values_need_review_and_never_raise(self):
        bad = [
            good_record(impact=["direct"]),
            good_record(policy_area={"a": 1}),
            good_record(citations=[{"field": "abstract", "quote": "a b c d", "supports": ["x"]}]),
            good_record(deadlines=[{"kind": ["comment_period_closes"], "date": "2026-11-09"}]),
        ]
        for rec in bad:
            with self.subTest(rec=rec):
                self.assertEqual(self.run_one(rec)["status"], "needs_review")

    def test_direct_without_assessment_needs_review(self):
        rec = good_record()
        del rec["assessment"]
        self.assertIn("assessment_missing", self.reasons(rec))
        rec["impact"] = "adjacent"
        self.assertNotIn("assessment_missing", self.reasons(rec))

    def test_injection_flag_sends_record_to_review(self):
        self.assertIn("injection_flagged", self.reasons(good_record(flags=["injection_attempt"])))

    # -- citations
    def test_citation_checks(self):
        self.assertIn("missing_citation", self.reasons(good_record(citations=[], signals=[])))
        rec = good_record()
        rec["citations"][0]["field"] = "body"
        self.assertIn("citation_field_unknown", self.reasons(rec))
        rec = good_record()
        rec["citations"][0]["text"] = "States"
        self.assertIn("citation_too_short", self.reasons(rec))
        rec = good_record()
        rec["citations"][1]["supports"] = "impact"
        self.assertIn("signal_uncited", self.reasons(rec))

    def test_nested_citations_in_changes_and_deadlines_are_checked(self):
        rec = good_record(changes_for_agencies=[
            {"point": "States verify identity.", "citation": {"field": "abstract", "text": "States must verify identity always"}}])
        self.assertIn("citation_not_verbatim", self.reasons(rec))

    # -- dates and dockets
    def test_dates_are_checked_against_the_source(self):
        self.assertIn("date_mismatch", self.reasons(good_record(comments_close_on="2026-11-16")))
        self.assertIn("date_mismatch", self.reasons(good_record(effective_on="2026-11-01")))
        self.assertIn("date_mismatch", self.reasons(good_record(comments_close_on=None)))
        self.assertIn("date_invalid", self.reasons(good_record(published="10/09/2026")))
        self.assertIn("date_invalid", self.reasons(good_record(published="2026-02-30")))

    def test_dockets_are_checked_against_the_source(self):
        self.assertIn("docket_mismatch", self.reasons(good_record(docket_ids=["EXAMPLE-2026-0999"])))
        self.assertIn("docket_mismatch", self.reasons(good_record(docket_ids=[])))

    def test_deadlines_are_checked(self):
        cit = {"field": "abstract", "text": "States would verify each applicant's identity"}
        ok = {"kind": "comment_period_closes", "date": "2026-12-08", "docket_id": "EXAMPLE-2026-0001", "citation": cit}
        self.assertEqual(self.reasons(good_record(deadlines=[ok])), [])
        self.assertIn("date_mismatch", self.reasons(good_record(deadlines=[dict(ok, date="2026-12-01")])))
        self.assertIn("docket_mismatch", self.reasons(good_record(deadlines=[dict(ok, docket_id="X-1")])))
        self.assertIn("enum_not_in_pack", self.reasons(good_record(deadlines=[dict(ok, kind="hearing")])))

    # -- the source record
    def test_source_lookup_from_items_and_missing_source(self):
        rec = good_record()
        raw = rec.pop("source_record")
        item = {"id": rec["id"], "doc_id": raw["document_number"], "title": raw["title"],
                "published": raw["publication_date"], "comments_close_on": raw["comments_close_on"],
                "effective_on": None, "docket_ids": raw["docket_ids"],
                "text": {"title": raw["title"], "abstract": raw["abstract"]}}
        out = self.run_one(rec, {rec["id"]: item})
        self.assertEqual(out["validation"]["reasons"], [])
        self.assertEqual(out["validation"]["source"], "items")
        self.assertEqual(self.reasons(rec), ["source_not_found"])

    def test_source_fixture_outside_the_fixtures_root_is_refused(self):
        rec = good_record(source_fixture="zero/policies.yaml")
        del rec["source_record"]
        self.assertIn("source_not_found", self.reasons(rec))

    def test_doc_id_must_match_the_source(self):
        self.assertIn("source_not_found", self.reasons(good_record(doc_id="2026-00000")))

    # -- the T207 sample
    def test_sample_records_match_their_expectation(self):
        recs = load_sample()
        results = validate.validate_all([("record", r, None) for r in recs], self.pack, validate.SourceIndex())
        self.assertEqual(len(results), len(recs), "a record was dropped")
        for rec, out in zip(recs, results):
            exp = rec["_expect"]
            with self.subTest(id=rec["id"]):
                if exp["result"] == "valid":
                    self.assertEqual(out["status"], "draft", out["validation"]["problems"])
                else:
                    self.assertEqual(out["status"], "needs_review")
                    self.assertIn(exp["reason"], out["validation"]["reasons"])
                    self.assertTrue(out["review_reason"].startswith(exp["reason"]) or exp["reason"] in out["review_reason"])

    def test_sample_as_committed_keeps_every_record(self):
        recs = load_sample(translate=False)
        results = validate.validate_all([("record", r, None) for r in recs], self.pack, validate.SourceIndex())
        self.assertEqual([r["id"] for r in results], [r["id"] for r in recs])
        self.assertTrue(all(r["status"] in ("draft", "needs_review") for r in results))

    # -- the command line
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = validate.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_cli_exits_zero_with_needs_review_and_keeps_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            src = pathlib.Path(d) / "in.jsonl"
            lines = [json.dumps(r) for r in load_sample(translate=False)]
            lines.insert(2, "{not json")
            src.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
            dst = pathlib.Path(d) / "sub" / "validated.jsonl"
            rc, out, _ = self.run_main(str(src), "--out", str(dst))
            self.assertEqual(rc, 0)
            results = [json.loads(ln) for ln in dst.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(results), len(lines))
            self.assertEqual(results[2]["status"], "needs_review")
            self.assertEqual(results[2]["validation"]["reasons"], ["unparseable_record"])
            self.assertEqual(results[2]["raw"], "{not json")
            summary = json.loads(out)
            self.assertEqual(summary["records"], len(lines))

    def test_cli_accepts_a_sort_answer_object(self):
        with tempfile.TemporaryDirectory() as d:
            rec = copy.deepcopy(good_record())
            rec["item_id"] = rec.pop("id")
            src = pathlib.Path(d) / "answer.json"
            src.write_text(json.dumps({"records": [rec]}), encoding="utf-8")
            dst = pathlib.Path(d) / "v.jsonl"
            rc, out, _ = self.run_main(str(src), "--out", str(dst))
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["valid"], 1)

    def test_cli_exits_nonzero_only_on_unreadable_input(self):
        with tempfile.TemporaryDirectory() as d:
            dst = pathlib.Path(d) / "v.jsonl"
            rc, _, err = self.run_main(str(pathlib.Path(d) / "missing.jsonl"), "--out", str(dst))
            self.assertEqual(rc, 2)
            self.assertIn("cannot read", err)
            self.assertFalse(dst.exists())
            bad = pathlib.Path(d) / "latin1.jsonl"
            bad.write_bytes(b"\xff\xfe\x00bad")
            self.assertEqual(self.run_main(str(bad), "--out", str(dst))[0], 2)
            self.assertEqual(self.run_main(str(SAMPLE), "--pack", d, "--out", str(dst))[0], 2)
            self.assertFalse(dst.exists())


if __name__ == "__main__":
    unittest.main()
