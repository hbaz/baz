#!/usr/bin/env python3
"""Validate classified records before they are used (standard library only).

    python3 zero/validate.py RECORDS [--pack DIR] [--items FILE ...]
                             [--fixtures DIR] [--out FILE]

RECORDS is JSON Lines (one classified record per line) or one JSON object
with a ``records`` list (a sort step answer). Every record is checked:

  1. the shape: an id (``id`` or ``item_id``), ``policy_area``, ``impact``,
     ``stage``, a one-line ``reason`` and a ``citations`` list whose entries
     carry ``field`` and the passage (``quote`` or ``text``); optional lists
     ``signals``, ``committees``, ``flags``, ``docket_ids``, ``deadlines``,
     ``changes_for_agencies`` are checked when present;
  2. the pack's values: policy areas (plus the fallback) and stages from
     ``taxonomy.yaml``, impact levels and escalation signals from
     ``impact.yaml``, committees from ``watchlist.yaml``, deadline kinds; a
     level with ``requires_assessment`` needs an ``assessment`` or ``summary``;
  3. every cited passage appears verbatim in the cited field of the fetched
     text (runs of whitespace are treated as one space; nothing else is
     forgiven: no case folding, no punctuation or spelling fixes);
  4. the dates (``published``, ``comments_close_on``, ``effective_on`` and
     each deadline date) and the docket ids against the source record.

The source record is looked up, in order: embedded in the record
(``source_record``: the raw API record, or ``item``: a normalized item),
by id in the ``--items`` files (``scan.py``'s ``new_items.jsonl``), then by
the record's ``source_fixture`` path (only under ``--fixtures``). A record
whose source cannot be found cannot be checked, so it needs review.

A record that fails any check gets ``status: needs_review`` and a
``review_reason``; a record that passes gets ``status: draft`` (the status is
set here, never taken from the model). Every record also gets a
``validation`` block with the result and each problem. Nothing is dropped:
a line that is not JSON becomes a ``needs_review`` stub holding the raw line.

Outputs ``--out`` (default ``out/validated.jsonl``; ``-`` for stdout) and
prints a one-line JSON summary to stdout (stderr when ``--out -``).

Exit status: 0 whenever the input could be read, however many records need
review; 2 when the records file, an ``--items`` file or the pack cannot be
read (nothing is written), or when ``--out`` cannot be written.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import sys
import tempfile

ZERO = pathlib.Path(__file__).resolve().parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402

REPO = ZERO.parent
DEFAULT_PACK = ZERO / "pack"
DEFAULT_FIXTURES = ZERO / "fixtures"
DEFAULT_OUT = REPO / "out" / "validated.jsonl"

STATUS_VALID = "draft"
STATUS_REVIEW = "needs_review"
ALLOWED_FLAGS = ("injection_attempt",)
MIN_QUOTE_WORDS = 3
MAX_REASON_WORDS = 30
DATE_FIELDS = ("published", "comments_close_on", "effective_on")
DEADLINE_DATE_FIELD = {"comment_period_closes": "comments_close_on", "effective_date": "effective_on"}
RAW_TEXT_FIELDS = ("title", "abstract", "action", "dates", "excerpts", "body",
                   "subject_1", "subject_2", "subject_3", "toc_subject", "toc_doc", "summary")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS = re.compile(r"\s+")

# Problem codes (the first one found becomes the head of review_reason).
SCHEMA_INVALID = "schema_invalid"
ENUM_NOT_IN_PACK = "enum_not_in_pack"
MISSING_CITATION = "missing_citation"
CITATION_NOT_VERBATIM = "citation_not_verbatim"
CITATION_FIELD_UNKNOWN = "citation_field_unknown"
CITATION_TOO_SHORT = "citation_too_short"
SIGNAL_UNCITED = "signal_uncited"
ASSESSMENT_MISSING = "assessment_missing"
SOURCE_NOT_FOUND = "source_not_found"
DATE_INVALID = "date_invalid"
DATE_MISMATCH = "date_mismatch"
DOCKET_MISMATCH = "docket_mismatch"
INJECTION_FLAGGED = "injection_flagged"
DUPLICATE_ID = "duplicate_id"
UNPARSEABLE = "unparseable_record"


class InputError(Exception):
    """The records file, an items file or the pack cannot be read."""


# ---------------------------------------------------------------------------
# The pack


def _ids(entries, what, path):
    if not isinstance(entries, list):
        raise InputError(f"{path}: {what} is not a list")
    out = []
    for e in entries:
        i = e.get("id") if isinstance(e, dict) else None
        if not isinstance(i, str) or not i:
            raise InputError(f"{path}: an entry of {what} has no id")
        out.append(i)
    if not out:
        raise InputError(f"{path}: {what} is empty")
    return out


def _load(path):
    try:
        doc = base.load_yaml(path)
    except (OSError, UnicodeDecodeError, base.YamlError) as e:
        raise InputError(f"cannot read {path}: {e}") from e
    if not isinstance(doc, dict):
        raise InputError(f"{path} is not a mapping")
    return doc


class Pack:
    """The closed vocabularies a classified record may use."""

    def __init__(self, root=DEFAULT_PACK):
        root = pathlib.Path(root)
        tax_p, imp_p, watch_p = root / "taxonomy.yaml", root / "impact.yaml", root / "watchlist.yaml"
        tax, imp = _load(tax_p), _load(imp_p)
        self.policy_areas = _ids(tax.get("policy_areas"), "policy_areas", tax_p)
        fb = tax.get("fallback_policy_area")
        if isinstance(fb, dict) and isinstance(fb.get("id"), str):
            self.policy_areas.append(fb["id"])
        self.stages = _ids(tax.get("stages"), "stages", tax_p)
        self.deadline_kinds = (_ids(tax.get("deadline_kinds"), "deadline_kinds", tax_p)
                               if tax.get("deadline_kinds") is not None else list(DEADLINE_DATE_FIELD))
        levels = imp.get("levels")
        self.impacts = _ids(levels, "levels", imp_p)
        self.requires_assessment = {l["id"] for l in levels if l.get("requires_assessment") is True}
        sig = imp.get("escalation_signals")
        self.signals = _ids(sig, "escalation_signals", imp_p) if sig is not None else []
        self.committees = []
        if watch_p.exists():
            watch = _load(watch_p)
            if watch.get("committees") is not None:
                self.committees = _ids(watch["committees"], "committees", watch_p)


# ---------------------------------------------------------------------------
# Source records


def _norm_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


class SourceView:
    """What a record is checked against: the fetched text, dates and dockets."""

    def __init__(self, doc_id, text, dates, dockets, origin):
        self.doc_id = doc_id
        self.text = text            # field -> str
        self.dates = dates          # field -> ISO str or None (key present = known)
        self.dockets = dockets      # list of str, or None when the source has no docket field
        self.origin = origin

    @classmethod
    def from_raw(cls, raw: dict, origin: str):
        """A raw Federal Register (or Public Inspection) API record."""
        text = {}
        for f in RAW_TEXT_FIELDS:
            v = raw.get("_body") if f == "body" and raw.get("body") is None else raw.get(f)
            if isinstance(v, list):
                v = " ".join(x for x in v if isinstance(x, str))
            if isinstance(v, str) and v.strip():
                text[f] = v
        dates = {}
        if "publication_date" in raw:
            dates["published"] = raw.get("publication_date")
        for f in ("comments_close_on", "effective_on"):
            if f in raw:
                dates[f] = raw.get(f)
        dockets = None
        for f in ("docket_ids", "docket_numbers"):
            if isinstance(raw.get(f), list):
                dockets = [d for d in raw[f] if isinstance(d, str)]
                break
        return cls(raw.get("document_number"), text, dates, dockets, origin)

    @classmethod
    def from_item(cls, item: dict, origin: str):
        """A normalized item as scan.py writes it."""
        text = {}
        for k, v in (item.get("text") or {}).items():
            if isinstance(v, str) and v.strip():
                text[k] = v
        if isinstance(item.get("title"), str) and "title" not in text:
            text["title"] = item["title"]
        dates = {f: item.get(f) for f in DATE_FIELDS if f in item}
        dockets = [d for d in item["docket_ids"] if isinstance(d, str)] \
            if isinstance(item.get("docket_ids"), list) else None
        return cls(item.get("doc_id"), text, dates, dockets, origin)


def _within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class SourceIndex:
    def __init__(self, items=None, fixtures_root=DEFAULT_FIXTURES):
        self.items = items or {}
        self.fixtures_root = pathlib.Path(fixtures_root)
        self._fixture_cache = {}

    def _fixture_results(self, rel):
        p = pathlib.Path(rel)
        if not p.is_absolute():
            p = REPO / p
        if not _within(p, self.fixtures_root):
            return None, f"source_fixture {rel!r} is outside {self.fixtures_root}"
        key = str(p.resolve())
        if key not in self._fixture_cache:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                self._fixture_cache[key] = (None, f"cannot read source_fixture {rel!r}: {e}")
            else:
                resp = doc.get("response") if isinstance(doc, dict) else None
                res = resp.get("results") if isinstance(resp, dict) else resp
                self._fixture_cache[key] = (res if isinstance(res, list) else [], None)
        return self._fixture_cache[key]

    def find(self, record: dict, rid):
        """(SourceView or None, why not found)."""
        doc_id = record.get("doc_id")
        if isinstance(record.get("source_record"), dict):
            return SourceView.from_raw(record["source_record"], "source_record"), None
        if isinstance(record.get("item"), dict):
            return SourceView.from_item(record["item"], "item"), None
        if isinstance(rid, str) and rid in self.items:
            return SourceView.from_item(self.items[rid], "items"), None
        rel = record.get("source_fixture")
        if isinstance(rel, str) and rel:
            results, err = self._fixture_results(rel)
            if err:
                return None, err
            for r in results:
                if isinstance(r, dict) and doc_id and r.get("document_number") == doc_id:
                    return SourceView.from_raw(r, rel), None
            return None, f"doc_id {doc_id!r} is not in {rel}"
        return None, f"no source record for {rid!r} (no item, source_record or source_fixture)"


# ---------------------------------------------------------------------------
# Checks


class Problems(list):
    def add(self, code, detail):
        self.append({"code": code, "detail": detail})


def _is_str(v):
    return isinstance(v, str) and v.strip() != ""


def _str_list(record, key, problems):
    v = record.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        problems.add(SCHEMA_INVALID, f"{key} must be a list of strings")
        return []
    return v


def _passage(c):
    return c.get("quote") if c.get("quote") is not None else c.get("text")


def check_citation(c, where, source, problems):
    if not isinstance(c, dict):
        problems.add(SCHEMA_INVALID, f"{where} is not an object")
        return
    field, passage = c.get("field"), _passage(c)
    if not _is_str(field) or not _is_str(passage):
        problems.add(SCHEMA_INVALID, f"{where} needs a field and a non-empty quote")
        return
    if len(passage.split()) < MIN_QUOTE_WORDS:
        problems.add(CITATION_TOO_SHORT, f"{where} quotes fewer than {MIN_QUOTE_WORDS} words: {passage!r}")
    if source is None:
        return
    text = source.text.get(field)
    if text is None:
        problems.add(CITATION_FIELD_UNKNOWN,
                     f"{where} cites field {field!r}, not in the fetched text ({', '.join(sorted(source.text)) or 'none'})")
        return
    if _norm_ws(passage) not in _norm_ws(text):
        problems.add(CITATION_NOT_VERBATIM, f"{where} is not verbatim in {field!r}: {passage!r}")


def _iso(v):
    if not isinstance(v, str) or not _ISO.match(v):
        return False
    try:
        _dt.date.fromisoformat(v)
        return True
    except ValueError:
        return False


def check_date(name, value, source, problems):
    if value is not None and not _iso(value):
        problems.add(DATE_INVALID, f"{name} is not YYYY-MM-DD: {value!r}")
        return
    if source is None or name not in source.dates:
        return
    if value != source.dates[name]:
        problems.add(DATE_MISMATCH, f"{name} is {value!r}; the source record says {source.dates[name]!r}")


def validate_record(record, pack: Pack, sources: SourceIndex, seen_ids=None) -> dict:
    """Return the record with status, review_reason and validation set. Never raises."""
    problems = Problems()
    if not isinstance(record, dict):
        return _stub(record, f"record is a {type(record).__name__}, not an object")
    out = dict(record)

    rid = record.get("id") if record.get("id") is not None else record.get("item_id")
    if not _is_str(rid):
        problems.add(SCHEMA_INVALID, "record has no id (id or item_id)")
        rid = None
    elif seen_ids is not None:
        if rid in seen_ids:
            problems.add(DUPLICATE_ID, f"id {rid!r} appears more than once in the input")
        seen_ids.add(rid)
    for key in ("source", "title", "doc_id"):
        if key in record and record[key] is not None and not isinstance(record[key], str):
            problems.add(SCHEMA_INVALID, f"{key} must be a string")

    # shape and pack values
    for key, allowed in (("policy_area", pack.policy_areas), ("impact", pack.impacts), ("stage", pack.stages)):
        v = record.get(key)
        if not _is_str(v):
            problems.add(SCHEMA_INVALID, f"record has no {key}")
        elif v not in allowed:
            problems.add(ENUM_NOT_IN_PACK, f"{key} {v!r} is not in the pack ({', '.join(allowed)})")
    reason = record.get("reason")
    if not _is_str(reason):
        problems.add(SCHEMA_INVALID, "record has no reason")
    elif len(reason.split()) > MAX_REASON_WORDS:
        problems.add(SCHEMA_INVALID, f"reason is longer than {MAX_REASON_WORDS} words")
    signals = _str_list(record, "signals", problems)
    for s in signals:
        if s not in pack.signals:
            problems.add(ENUM_NOT_IN_PACK, f"signal {s!r} is not in the pack")
    for c in _str_list(record, "committees", problems):
        if c not in pack.committees:
            problems.add(ENUM_NOT_IN_PACK, f"committee {c!r} is not in the pack")
    flags = _str_list(record, "flags", problems)
    for f in flags:
        if f not in ALLOWED_FLAGS:
            problems.add(ENUM_NOT_IN_PACK, f"flag {f!r} is not allowed")
    if "injection_attempt" in flags:
        problems.add(INJECTION_FLAGGED, "the classifier flagged an injection attempt in the item text")
    if isinstance(record.get("impact"), str) and record["impact"] in pack.requires_assessment:
        if not (_is_str(record.get("assessment")) or _is_str(record.get("summary"))):
            problems.add(ASSESSMENT_MISSING, f"impact {record.get('impact')!r} needs an assessment")

    # the source record
    source, why = sources.find(record, rid)
    if source is None:
        problems.add(SOURCE_NOT_FOUND, why)
    elif record.get("doc_id") and source.doc_id and record["doc_id"] != source.doc_id:
        problems.add(SOURCE_NOT_FOUND, f"doc_id {record['doc_id']!r} does not match the source {source.doc_id!r}")
        source = None

    # citations
    cits = record.get("citations")
    if not isinstance(cits, list):
        problems.add(SCHEMA_INVALID if cits is not None else MISSING_CITATION, "citations must be a list")
        cits = []
    if not cits:
        problems.add(MISSING_CITATION, "record has no citation")
    for i, c in enumerate(cits):
        check_citation(c, f"citations[{i}]", source, problems)
    supported = {c.get("supports") for c in cits if isinstance(c, dict) and isinstance(c.get("supports"), str)}
    for s in signals:
        if s not in supported:
            problems.add(SIGNAL_UNCITED, f"signal {s!r} has no citation with supports: {s}")

    changes = record.get("changes_for_agencies")
    if changes is not None:
        if not isinstance(changes, list):
            problems.add(SCHEMA_INVALID, "changes_for_agencies must be a list")
        else:
            for i, ch in enumerate(changes):
                if not isinstance(ch, dict) or not _is_str(ch.get("point")):
                    problems.add(SCHEMA_INVALID, f"changes_for_agencies[{i}] needs a point")
                    continue
                if ch.get("citation") is None:
                    problems.add(MISSING_CITATION, f"changes_for_agencies[{i}] has no citation")
                else:
                    check_citation(ch["citation"], f"changes_for_agencies[{i}].citation", source, problems)

    # dates and dockets
    for name in DATE_FIELDS:
        if name in record:
            check_date(name, record[name], source, problems)
    src_dockets = set(source.dockets) if source is not None and source.dockets is not None else None
    if "docket_ids" in record:
        dockets = _str_list(record, "docket_ids", problems)
        if src_dockets is not None and set(dockets) != src_dockets:
            problems.add(DOCKET_MISMATCH,
                         f"docket_ids {sorted(set(dockets))} do not match the source record {sorted(src_dockets)}")
    deadlines = record.get("deadlines")
    if deadlines is not None:
        if not isinstance(deadlines, list):
            problems.add(SCHEMA_INVALID, "deadlines must be a list")
            deadlines = []
        for i, d in enumerate(deadlines):
            where = f"deadlines[{i}]"
            if not isinstance(d, dict):
                problems.add(SCHEMA_INVALID, f"{where} is not an object")
                continue
            kind = d.get("kind")
            if kind not in pack.deadline_kinds:
                problems.add(ENUM_NOT_IN_PACK, f"{where}.kind {kind!r} is not in the pack")
            date = d.get("date")
            if not _iso(date):
                problems.add(DATE_INVALID, f"{where}.date is not YYYY-MM-DD: {date!r}")
            elif source is not None and isinstance(kind, str) and DEADLINE_DATE_FIELD.get(kind) in source.dates:
                want = source.dates[DEADLINE_DATE_FIELD[kind]]
                if date != want:
                    problems.add(DATE_MISMATCH, f"{where}.date is {date!r}; the source record says {want!r}")
            dk = d.get("docket_id")
            if dk is not None and (not isinstance(dk, str) or (src_dockets is not None and dk not in src_dockets)):
                problems.add(DOCKET_MISMATCH, f"{where}.docket_id {dk!r} is not in the source record")
            if d.get("citation") is None:
                problems.add(MISSING_CITATION, f"{where} has no citation")
            else:
                check_citation(d["citation"], f"{where}.citation", source, problems)

    return _finish(out, problems, source)


def _finish(out, problems, source=None):
    codes = []
    for p in problems:
        if p["code"] not in codes:
            codes.append(p["code"])
    ok = not problems
    out["status"] = STATUS_VALID if ok else STATUS_REVIEW
    if ok:
        out.pop("review_reason", None)
    else:
        out["review_reason"] = "; ".join(f"{p['code']}: {p['detail']}" for p in problems)
    out["validation"] = {
        "result": "valid" if ok else STATUS_REVIEW,
        "reasons": codes,
        "problems": list(problems),
        "source": source.origin if source is not None else None,
    }
    return out


def _stub(raw, detail, line=None):
    p = Problems()
    p.add(UNPARSEABLE, detail)
    out = {"raw": raw}
    if line is not None:
        out["line"] = line
    return _finish(out, p)


# ---------------------------------------------------------------------------
# Input and output


def _read_text(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise InputError(f"cannot read {path}: {e}") from e


def read_records(path) -> list:
    """Return a list of (record or stub) in input order. Raises InputError if unreadable."""
    text = _read_text(path)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("records"), list):
            return [("record", r, None) for r in doc["records"]]
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(("record", json.loads(line), n))
        except json.JSONDecodeError as e:
            out.append(("stub", line, (n, f"line {n} is not JSON: {e.msg}")))
    return out


def read_items(paths) -> dict:
    items = {}
    for path in paths or []:
        for n, line in enumerate(_read_text(path).splitlines(), 1):
            if not line.strip():
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError as e:
                raise InputError(f"{path} line {n} is not JSON: {e.msg}") from e
            if isinstance(it, dict) and isinstance(it.get("id"), str):
                items.setdefault(it["id"], it)
    return items


def validate_all(entries, pack, sources) -> list:
    seen = set()
    out = []
    for kind, value, extra in entries:
        if kind == "stub":
            line, detail = extra
            out.append(_stub(value, detail, line))
        else:
            out.append(validate_record(value, pack, sources, seen))
    return out


def summarize(results) -> dict:
    reasons = {}
    for r in results:
        for c in r["validation"]["reasons"]:
            reasons[c] = reasons.get(c, 0) + 1
    return {
        "records": len(results),
        "valid": sum(1 for r in results if r["status"] == STATUS_VALID),
        "needs_review": sum(1 for r in results if r["status"] == STATUS_REVIEW),
        "reasons": dict(sorted(reasons.items())),
    }


def write_jsonl(path, records):
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_args(argv):
    p = argparse.ArgumentParser(prog="validate.py", description="Validate classified records.")
    p.add_argument("records", help="classified records (JSON Lines, or a JSON object with a records list)")
    p.add_argument("--pack", default=str(DEFAULT_PACK), help="pack folder (default: zero/pack)")
    p.add_argument("--items", action="append", default=[], help="normalized items (new_items.jsonl); repeatable")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="root that source_fixture paths must be under")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output file (default: out/validated.jsonl; - for stdout)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        pack = Pack(args.pack)
        entries = read_records(args.records)
        items = read_items(args.items)
    except InputError as e:
        print(f"validate: {e}", file=sys.stderr)
        return 2
    results = validate_all(entries, pack, SourceIndex(items, args.fixtures))
    summary = summarize(results)
    if args.out == "-":
        sys.stdout.write("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in results))
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    else:
        try:
            write_jsonl(args.out, results)
        except OSError as e:
            print(f"validate: cannot write {args.out}: {e}", file=sys.stderr)
            return 2
        print(json.dumps(dict(summary, out=str(args.out)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
