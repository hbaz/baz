#!/usr/bin/env python3
"""Scan the enabled sources and write the new items (standard library only).

    python3 zero/scan.py [--sim] [--out DIR] [--known FILE] [--today YYYY-MM-DD]
                         [--lookback-days N] [--sources ID,ID] [--policies FILE]

Steps, for every source id in ``sources.enabled`` of ``zero/policies.yaml``
(in that order):

  1. load ``zero/pack/sources/{id}.yaml``;
  2. run it through the connector registered for its ``kind`` over the
     lookback window (``run.lookback_days`` days ending ``--today``);
  3. check every item against the item shape (the core fields below);
  4. dedupe by item ``id`` against the known ids (``--known``) and against
     the items already taken earlier in this run (first source wins, so the
     published ``fedreg`` record is kept over the Public Inspection one that
     shares its id).

Outputs, in ``--out`` (default ``out/`` at the repository root):

  - ``new_items.jsonl``: one new item per line;
  - ``run_summary.json``: the window, the totals and one entry per source with
    its ``status`` (``ok``, ``partial``, ``failed`` or ``disabled``), counts and
    every recorded error.

Nothing is skipped silently: a missing or unreadable source file, an unknown
kind, a refused (non-public) source, a failed page, a record that cannot be
normalized and an item missing a core field each become an error entry for
that source. A source listed as enabled whose file says ``enabled: false``
is reported as ``disabled`` with the reason, not fetched.

``--sim`` replays the fixtures under ``zero/fixtures/`` instead of the
network; without ``--today`` the window then ends on the latest date the
fixtures hold, so the simulator always has data to scan.

Exit status: 0 when every source is ok, partial or disabled; 1 when the
outputs were written but at least one source failed; 2 on bad usage or an
unreadable policies or known-ids file (nothing is written).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sys
import tempfile

ZERO = pathlib.Path(__file__).resolve().parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402

REPO = ZERO.parent
DEFAULT_OUT = REPO / "out"
DEFAULT_POLICIES = ZERO / "policies.yaml"
SOURCES_DIR = base.PACK_ROOT / "sources"
ITEMS_FILE = "new_items.jsonl"
SUMMARY_FILE = "run_summary.json"
DEFAULT_LOOKBACK_DAYS = 3
# The core of the item shape every connector's normalize must produce.
REQUIRED_FIELDS = ("id", "source", "kind", "title")


class UsageError(Exception):
    """Bad arguments or an unreadable policies or known-ids file."""


# ---------------------------------------------------------------------------
# Inputs


def load_policies(path) -> dict:
    try:
        doc = base.load_yaml(path)
    except (OSError, base.YamlError) as e:
        raise UsageError(f"cannot read policies {path}: {e}") from e
    if not isinstance(doc, dict):
        raise UsageError(f"policies {path} is not a mapping")
    return doc


def enabled_ids(policies: dict) -> list:
    ids = ((policies.get("sources") or {}).get("enabled")) or []
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        raise UsageError("sources.enabled must be a list of source ids")
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def lookback_days(policies: dict) -> int:
    v = (policies.get("run") or {}).get("lookback_days", DEFAULT_LOOKBACK_DAYS)
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise UsageError(f"run.lookback_days is not a whole number: {v!r}") from e
    if n < 1:
        raise UsageError("run.lookback_days must be at least 1")
    return n


def load_known_ids(path) -> set:
    """A JSON list of ids, a JSON object with an ``ids`` list, or one id per line."""
    if path is None:
        return set()
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise UsageError(f"cannot read known ids {path}: {e}") from e
    stripped = text.strip()
    if not stripped:
        return set()
    if stripped[0] in "[{":
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise UsageError(f"known ids {path} is not valid JSON: {e}") from e
        if isinstance(doc, dict):
            doc = doc.get("ids")
        if not isinstance(doc, list) or not all(isinstance(i, str) for i in doc):
            raise UsageError(f"known ids {path} must be a list of strings")
        return set(doc)
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def latest_fixture_date(root=base.FIXTURES_ROOT):
    """The latest ISO date any fixture request asks for (the simulator's 'today')."""
    latest = None
    for key in base.FixtureTransport(root).index:
        for d in key[1]:
            try:
                day = _dt.date.fromisoformat(d)
            except ValueError:
                continue
            if latest is None or day > latest:
                latest = day
    return latest


# ---------------------------------------------------------------------------
# The scan


def _error(sid, kind, stage, err, url=None) -> dict:
    return {"source": sid, "kind": kind, "stage": stage, "url": url, "error": str(err)}


def _entry(sid: str) -> dict:
    return {
        "id": sid, "kind": None, "status": None, "reason": None,
        "raw_count": 0, "item_count": 0, "new_count": 0,
        "known_count": 0, "duplicate_count": 0, "invalid_count": 0,
        "truncated": False, "errors": [],
    }


def check_item(item) -> list:
    if not isinstance(item, dict):
        return ["item is not an object"]
    return [f"item has no {f!r}" for f in REQUIRED_FIELDS if not item.get(f)]


def scan_source(sid, window, fetcher_factory, known, taken, scanned_at, sources_dir=SOURCES_DIR):
    """Run one source. Returns (summary entry, new items). Never raises for a source fault."""
    entry = _entry(sid)
    new = []
    path = pathlib.Path(sources_dir) / f"{sid}.yaml"
    try:
        src = base.load_source_file(path)
    except (OSError, base.YamlError) as e:
        entry["status"] = "failed"
        entry["errors"].append(_error(sid, None, "load", f"cannot read {path.name}: {e}"))
        return entry, new
    entry["kind"] = src.get("kind")
    if src.get("id") != sid:
        entry["status"] = "failed"
        entry["errors"].append(_error(sid, entry["kind"], "load", f"{path.name} declares id {src.get('id')!r}"))
        return entry, new
    if src.get("enabled") is False:
        entry["status"] = "disabled"
        entry["reason"] = f"{path.name} has enabled: false" + (
            f" (status: {src['status']})" if src.get("status") else "")
        return entry, new
    try:
        conn = base.create(src, fetcher=fetcher_factory())
    except (KeyError, ValueError, TypeError) as e:
        entry["status"] = "failed"
        entry["errors"].append(_error(sid, entry["kind"], "load", e))
        return entry, new
    try:
        res = conn.run(window)
    except Exception as e:  # a connector bug must still be recorded, not lost
        entry["status"] = "failed"
        entry["errors"].append(_error(sid, entry["kind"], "fetch", f"{type(e).__name__}: {e}"))
        return entry, new
    entry["raw_count"] = res.raw_count
    entry["item_count"] = len(res.items)
    entry["truncated"] = bool(res.truncated)
    entry["errors"].extend(res.errors)
    for item in res.items:
        problems = check_item(item)
        if problems:
            entry["invalid_count"] += 1
            ident = item.get("id") if isinstance(item, dict) else None
            entry["errors"].append(_error(sid, entry["kind"], "schema", "; ".join(problems), ident))
            continue
        iid = item["id"]
        if iid in known:
            entry["known_count"] += 1
            continue
        if iid in taken:
            entry["duplicate_count"] += 1
            continue
        taken.add(iid)
        new.append(dict(item, scanned_at=scanned_at))
    entry["new_count"] = len(new)
    if not entry["errors"]:
        entry["status"] = "ok"
    elif entry["raw_count"] or entry["item_count"]:
        entry["status"] = "partial"
    else:
        entry["status"] = "failed"
    return entry, new


def scan(source_ids, window, fetcher_factory, known=frozenset(), sim=False,
         sources_dir=SOURCES_DIR, now=None):
    """Scan every source id in order. Returns (summary, new items)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    scanned_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    taken: set = set()
    entries, items = [], []
    for sid in source_ids:
        entry, new = scan_source(sid, window, fetcher_factory, known, taken, scanned_at, sources_dir)
        entries.append(entry)
        items.extend(new)
    counts = {s: sum(1 for e in entries if e["status"] == s) for s in ("ok", "partial", "failed", "disabled")}
    summary = {
        "scanned_at": scanned_at,
        "mode": "sim" if sim else "live",
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat(),
                   "days": (window.end - window.start).days + 1},
        "known_ids": len(known),
        "new_items": len(items),
        "sources_total": len(entries),
        "sources_by_status": counts,
        "failed_sources": [e["id"] for e in entries if e["status"] == "failed"],
        "sources": entries,
    }
    return summary, items


# ---------------------------------------------------------------------------
# Output


def _atomic_write(path: pathlib.Path, text: str):
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


def write_outputs(out_dir, summary, items):
    out_dir = pathlib.Path(out_dir)
    lines = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in items)
    _atomic_write(out_dir / ITEMS_FILE, lines)
    _atomic_write(out_dir / SUMMARY_FILE, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return out_dir / ITEMS_FILE, out_dir / SUMMARY_FILE


# ---------------------------------------------------------------------------
# CLI


def parse_args(argv):
    p = argparse.ArgumentParser(prog="scan.py", description="Scan the enabled sources for new items.")
    p.add_argument("--sim", action="store_true", help="replay zero/fixtures/ instead of the network")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: out/)")
    p.add_argument("--known", default=None, help="file of known item ids (JSON list or one per line)")
    p.add_argument("--today", default=None, help="last day of the window, YYYY-MM-DD")
    p.add_argument("--lookback-days", type=int, default=None, help="override run.lookback_days")
    p.add_argument("--sources", default=None, help="comma-separated source ids (override sources.enabled)")
    p.add_argument("--policies", default=str(DEFAULT_POLICIES), help="policies file")
    p.add_argument("--fixtures", default=str(base.FIXTURES_ROOT), help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        policies = load_policies(args.policies)
        ids = ([s.strip() for s in args.sources.split(",") if s.strip()]
               if args.sources is not None else enabled_ids(policies))
        if not ids:
            raise UsageError("no sources to scan")
        days = args.lookback_days if args.lookback_days is not None else lookback_days(policies)
        if days < 1:
            raise UsageError("--lookback-days must be at least 1")
        if args.today:
            try:
                today = _dt.date.fromisoformat(args.today)
            except ValueError as e:
                raise UsageError(f"--today is not YYYY-MM-DD: {args.today!r}") from e
        elif args.sim:
            today = latest_fixture_date(args.fixtures)
            if today is None:
                raise UsageError(f"no dated fixtures under {args.fixtures}")
        else:
            today = _dt.date.today()
        known = load_known_ids(args.known)
    except UsageError as e:
        print(f"scan: {e}", file=sys.stderr)
        return 2

    window = base.Window.lookback(days, today)
    # One fetcher for the whole run, so a host shared by several sources
    # (every fedreg source is www.federalregister.gov) keeps one delay and
    # one robots.txt read.
    if args.sim:
        fetcher = base.PoliteFetcher(transport=base.FixtureTransport(args.fixtures), sleep=lambda s: None)
    else:
        fetcher = base.PoliteFetcher()

    def factory():
        return fetcher

    summary, items = scan(ids, window, factory, known=known, sim=args.sim)
    items_path, summary_path = write_outputs(args.out, summary, items)
    for e in summary["sources"]:
        line = f"scan: {e['id']}: {e['status']}, {e['new_count']} new of {e['item_count']}"
        if e["errors"]:
            line += f", {len(e['errors'])} error(s)"
        if e["reason"]:
            line += f" ({e['reason']})"
        print(line, file=sys.stderr)
    print(f"scan: {summary['new_items']} new item(s) -> {items_path}; summary -> {summary_path}", file=sys.stderr)
    return 1 if summary["failed_sources"] else 0


if __name__ == "__main__":
    sys.exit(main())
