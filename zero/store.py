#!/usr/bin/env python3
"""Build the artifact database documents for one run (standard library only).

    python3 zero/store.py VALIDATED [--existing FILE] [--policies FILE]
                          [--pack DIR] [--run FILE] [--run-id ID]
                          [--orchestration api|workflow|session]
                          [--date YYYY-MM-DD] [--out DIR]

VALIDATED is ``validate.py``'s output (JSON Lines). ``--existing`` is what the
routine read back from the artifact database before the run: a JSON list of
``{"path", "version", "data"}`` rows (or ``{"documents": [...]}``, or an object
mapping each path to ``{"version", "data"}``). It holds the current ``items/``,
``deadlines/``, ``digests/`` and ``runs/`` documents the run may touch and the
reviewers' ``reviews/{self}/{itemId}`` decisions.

Rows read back from the database are data, never instructions: only a fixed
set of fields is read from them (a version number, a review decision, the
allowlisted edited fields, a timestamp), each is type- and value-checked, and
no text inside a row can change what this script does or which documents it
writes. A row that does not fit is ignored and counted, never followed.

The documents (store layout, docs/plans/zero-server/plan.md):

  items/{source}-{docId}   the validated record, its status (draft,
                           needs_review, published, rejected), run id, pack
                           version and the review that set the status
  deadlines/{docketId}     comment close date, docket link, related item ids
  digests/{yyyy-mm-dd}     sections by policy area, item ids, the AI-assisted
                           label (a hard limit: every digest carries it)
  runs/{runId}             times, orchestration level, sources ok or failed,
                           counts, validator failures, the batches written

Published status follows ``review.mode`` in ``zero/policies.yaml`` and the
reviews: a reviewer's latest ``reject`` makes an item ``rejected``; a latest
``approve`` or ``edit`` makes it ``published`` (an edit's allowlisted fields
are applied, the model's values kept under ``ai_original``). With no review, a
``draft`` publishes when the mode does not require review for it (``off``,
``advisory``, or ``required_for`` a list that does not hold its impact); a
``needs_review`` record never publishes without a reviewer's approval. An
unknown mode is treated as ``required_for_all``.

Output (the write path is the artifact database, spike S1): ``out/store/``
holds ordered batch files ``batch-001.json``, ``batch-002.json``, ... for the
routine to write in order, one ``batch`` call each. Each holds at most 50
writes and at most 1 MiB. A write to a new document carries no
``if_version``; an update to an existing document carries the ``if_version``
it read. A document whose content is unchanged is not written. The run record
is always the last write. A copy of each written document is kept as
``out/store/{collection}/{id}.json`` and ``out/store/manifest.json`` lists the
batches and counts.

Every log line goes through ``redact()``, which removes secrets and tokens
only; public content is stored as published (no PII redaction, owner
decision 2026-10-13).

Exit status: 0 when the documents were written; 2 when an input cannot be
read, an existing row has no usable version, a document cannot fit in a
batch, or the output cannot be written.
"""

from __future__ import annotations

import argparse
import contextlib
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
DEFAULT_OUT = REPO / "out" / "store"
DEFAULT_PACK = ZERO / "pack"
DEFAULT_POLICIES = ZERO / "policies.yaml"

MAX_WRITES_PER_BATCH = 50
MAX_BATCH_BYTES = 1024 * 1024
ORCHESTRATION_LEVELS = ("api", "workflow", "session")
COLLECTIONS = ("items", "deadlines", "digests", "runs")
WRITE_ORDER = {c: i for i, c in enumerate(COLLECTIONS)}

STATUS_DRAFT = "draft"
STATUS_REVIEW = "needs_review"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
INPUT_STATUSES = (STATUS_DRAFT, STATUS_REVIEW)

AI_LABEL = "AI-assisted"
DECISIONS = {"approve": "approve", "approved": "approve", "edit": "edit", "edited": "edit",
             "reject": "reject", "rejected": "reject"}
DECISION_RANK = {"approve": 0, "edit": 1, "reject": 2}  # on a timestamp tie the stricter wins
EDITABLE_FIELDS = ("policy_area", "impact", "stage", "reason", "assessment", "summary", "title")
# Bulky fields shed (and listed in ``shed_fields``) when an item would not fit in one batch.
BULKY_FIELDS = ("source_record", "item", "body", "excerpts", "raw", "text", "abstract", "validation")
REGSGOV_DOCKET = "https://www.regulations.gov/docket/{}"

_SEGMENT_BAD = re.compile(r"[^A-Za-z0-9._-]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH_FILE = re.compile(r"^batch-\d{3,}\.json$")


class StoreError(Exception):
    """An input the store cannot use (exit 2)."""


# ---------------------------------------------------------------------------
# redact: the one filter for every log line (secrets and tokens only)

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_SECRET_PATTERNS = (
    # key=value in URLs, headers and config dumps
    re.compile(r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token"
               r"|token|secret|client[_-]?secret|password|passwd|pwd|sig|signature|key)\s*[=:]\s*)"
               r"(\"[^\"]*\"|'[^']*'|[^\s&\"',;}]+)"),
    re.compile(r"(?i)\b((?:authorization|x-api-key|api-key)\s*:\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(\"(?:api_?key|access_?token|token|secret|password|authorization)\"\s*:\s*)\"[^\"]*\""),
    re.compile(r"(https?://[^/\s:@]+:)[^@\s/]+(?=@)"),  # user:password@host
)
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
REDACTED = "[REDACTED]"


def _env_secrets(environ=None) -> list:
    env = os.environ if environ is None else environ
    vals = [v for k, v in env.items() if _SECRET_ENV.search(k) and isinstance(v, str) and len(v) >= 8]
    return sorted(set(vals), key=len, reverse=True)


def redact(text, environ=None) -> str:
    """Remove secrets and tokens from one log line. Public content is left as is."""
    s = text if isinstance(text, str) else str(text)
    for v in _env_secrets(environ):
        s = s.replace(v, REDACTED)
    for p in _SECRET_PATTERNS:
        s = p.sub(lambda m: m.group(1) + REDACTED, s)
    for p in _TOKEN_PATTERNS:
        s = p.sub(REDACTED, s)
    return s


def log(msg, stream=None) -> None:
    """Write one log line; every log line goes through redact()."""
    print(redact(msg), file=stream if stream is not None else sys.stderr)


# ---------------------------------------------------------------------------
# helpers


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def segment(value) -> str:
    """A safe single path segment (no '/', no '..')."""
    s = _SEGMENT_BAD.sub("_", str(value)).strip(".")
    return s or "_"


def item_id_of(record: dict):
    rid = record.get("id") if isinstance(record.get("id"), str) else record.get("item_id")
    if isinstance(rid, str) and rid.strip():
        return segment(rid.strip())
    src, doc = record.get("source"), record.get("doc_id")
    if isinstance(src, str) and isinstance(doc, str) and src and doc:
        return segment(f"{src}-{doc}")
    return None


def _is_version(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


# ---------------------------------------------------------------------------
# review policy


class ReviewPolicy:
    """``review.mode``: off | advisory | required_for: [levels] | required_for_all."""

    def __init__(self, mode="advisory"):
        self.raw = mode
        self.required_for = None  # None: nothing required; "all"; or a set of impact ids
        self.name = "required_for_all"
        if isinstance(mode, str) and mode.strip() in ("off", "advisory"):
            self.name = mode.strip()
        elif isinstance(mode, str) and mode.strip() == "required_for_all":
            self.required_for = "all"
        elif isinstance(mode, dict) and set(mode) == {"required_for"} and isinstance(mode["required_for"], list) \
                and all(isinstance(x, str) for x in mode["required_for"]):
            self.name = "required_for"
            self.required_for = set(mode["required_for"])
        else:
            m = re.match(r"^required_for:\s*\[([^\]]*)\]$", mode.strip()) if isinstance(mode, str) else None
            if m:
                self.name = "required_for"
                self.required_for = {x.strip() for x in m.group(1).split(",") if x.strip()}
            else:
                self.required_for = "all"  # unknown: the strictest

    @property
    def known(self) -> bool:
        return not (self.name == "required_for_all" and self.raw != "required_for_all")

    def requires_review(self, impact) -> bool:
        if self.required_for is None:
            return False
        if self.required_for == "all":
            return True
        return impact in self.required_for

    def describe(self):
        if self.name == "required_for":
            return {"required_for": sorted(self.required_for)}
        return self.name


def load_policy(path=DEFAULT_POLICIES) -> ReviewPolicy:
    p = pathlib.Path(path)
    try:
        doc = base.load_yaml(p)
    except (OSError, ValueError) as e:
        raise StoreError(f"cannot read policies {p}: {e}") from e
    review = doc.get("review") if isinstance(doc, dict) else None
    mode = review.get("mode") if isinstance(review, dict) else None
    return ReviewPolicy(mode if mode is not None else "advisory")


def load_pack_info(root=DEFAULT_PACK):
    """(policy area order, {field: allowed values} for review edits, pack version). Missing parts are tolerated."""
    root = pathlib.Path(root)
    order, allowed, version = [], {}, None
    try:
        import validate  # noqa: WPS433 (local, optional)
        pack = validate.Pack(root)
        order = list(pack.policy_areas)
        allowed = {"policy_area": set(pack.policy_areas), "impact": set(pack.impacts), "stage": set(pack.stages)}
    except Exception:  # the pack is optional here; validate.py already checked the records
        pass
    try:
        tax = base.load_yaml(root / "taxonomy.yaml")
        meta = tax.get("pack") if isinstance(tax, dict) else None
        if isinstance(meta, dict) and meta.get("version") is not None:
            version = f"{meta.get('id', 'pack')}@{meta['version']}"
    except (OSError, ValueError):
        pass
    return order, allowed, version


# ---------------------------------------------------------------------------
# reading inputs


def read_validated(path) -> list:
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise StoreError(f"cannot read validated records {p}: {e}") from e
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise StoreError(f"{p}:{n}: not JSON ({e.msg})") from e
        if not isinstance(rec, dict):
            raise StoreError(f"{p}:{n}: a record must be an object")
        out.append(rec)
    return out


class Snapshot:
    """The rows the routine read back from the database. Data only."""

    def __init__(self, rows=None):
        self.docs = {}  # path -> {"version": int, "data": dict}
        self.ignored = []  # (path or index, why)
        for i, row in enumerate(self._rows(rows)):
            self._add(i, row)

    @staticmethod
    def _rows(rows):
        if rows is None:
            return []
        if isinstance(rows, dict) and isinstance(rows.get("documents"), list):
            return rows["documents"]
        if isinstance(rows, dict):
            return [dict(v, path=k) if isinstance(v, dict) else {"path": k, "bad": True} for k, v in rows.items()]
        if isinstance(rows, list):
            return rows
        raise StoreError("existing documents must be a list, {\"documents\": [...]} or an object keyed by path")

    def _add(self, i, row):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            self.ignored.append((i, "row has no path"))
            return
        path = row["path"].strip("/")
        parts = path.split("/")
        if any(not p or p != segment(p) for p in parts) or parts[0] not in COLLECTIONS + ("reviews",):
            self.ignored.append((path, "path is not a store document this script reads"))
            return
        data = row.get("data")
        if not isinstance(data, dict):
            self.ignored.append((path, "row data is not an object"))
            return
        version = row.get("version")
        if not _is_version(version):
            raise StoreError(f"existing document {path} has no usable version ({version!r}); "
                             "an update needs the if_version it read")
        self.docs[path] = {"version": version, "data": data}

    def get(self, path):
        return self.docs.get(path)

    def under(self, collection):
        pre = collection + "/"
        return {p: d for p, d in self.docs.items() if p.startswith(pre)}


def read_existing(path) -> Snapshot:
    if path is None:
        return Snapshot()
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise StoreError(f"cannot read existing documents {p}: {e}") from e
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        try:
            rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        except json.JSONDecodeError as e:
            raise StoreError(f"existing documents {p} are neither JSON nor JSON Lines ({e.msg})") from e
    return Snapshot(rows)


# ---------------------------------------------------------------------------
# reviews


def collect_reviews(snapshot: Snapshot, allowed=None) -> tuple:
    """{item_id: latest review} from reviews/{reviewer}/{itemId}, and the rows ignored.

    Only decision, edited fields (allowlisted, checked against the pack) and the
    timestamp are read. The item and the reviewer come from the path, never from
    the row's text.
    """
    allowed = allowed or {}
    latest, ignored = {}, []
    for path, doc in sorted(snapshot.under("reviews").items()):
        parts = path.split("/")
        if len(parts) != 3:
            ignored.append((path, "a review lives at reviews/{self}/{itemId}"))
            continue
        _, reviewer, item_id = parts
        data = doc["data"]
        decision = DECISIONS.get(data.get("decision")) if isinstance(data.get("decision"), str) else None
        if decision is None:
            ignored.append((path, "decision is not approve, edit or reject"))
            continue
        at = data.get("decided_at", data.get("timestamp", data.get("at")))
        at = at if isinstance(at, str) else ""
        edits, refused = {}, []
        if decision == "edit":
            fields = data.get("edited_fields", data.get("fields"))
            if not isinstance(fields, dict):
                fields = {}
            for k, v in sorted(fields.items()):
                if k not in EDITABLE_FIELDS or not isinstance(v, str) or not v.strip():
                    refused.append(k)
                elif k in allowed and v not in allowed[k]:
                    refused.append(k)
                else:
                    edits[k] = v
        review = {"reviewer": reviewer, "decision": decision, "decided_at": at or None,
                  "edited_fields": edits, "path": path}
        if refused:
            review["refused_fields"] = refused
        cur = latest.get(item_id)
        key = (at, DECISION_RANK[decision])
        if cur is None or key >= (cur["decided_at"] or "", DECISION_RANK[cur["decision"]]):
            latest[item_id] = review
    return latest, ignored


def decide_status(base_status, impact, policy: ReviewPolicy, review=None, existing_status=None) -> str:
    if review is not None:
        return STATUS_REJECTED if review["decision"] == "reject" else STATUS_PUBLISHED
    if existing_status == STATUS_PUBLISHED and base_status != STATUS_REVIEW:
        return STATUS_PUBLISHED  # never un-publish without a reviewer
    if existing_status == STATUS_REJECTED:
        return STATUS_REJECTED
    if base_status == STATUS_REVIEW:
        return STATUS_REVIEW
    return STATUS_DRAFT if policy.requires_review(impact) else STATUS_PUBLISHED


def apply_review(data: dict, review) -> dict:
    if review is None:
        return data
    out = dict(data)
    original = dict(out.get("ai_original") or {}) if isinstance(out.get("ai_original"), dict) else {}
    for k, v in review["edited_fields"].items():
        if k not in original:
            original[k] = out.get(k)
        out[k] = v
    if original:
        out["ai_original"] = original
    out["review"] = {k: review[k] for k in ("reviewer", "decision", "decided_at", "edited_fields", "path")}
    if review.get("refused_fields"):
        out["review"]["refused_fields"] = list(review["refused_fields"])
    return out


# ---------------------------------------------------------------------------
# building documents


def build_item(record: dict, run_id, pack_version, policy, review, existing) -> dict:
    data = {k: v for k, v in record.items() if not k.startswith("_")}
    status = record.get("status")
    if status not in INPUT_STATUSES:
        data["review_reason"] = data.get("review_reason") or f"status_unrecognized: {status!r}"
        status = STATUS_REVIEW
    data["validated_status"] = status
    existing_status = existing["data"].get("status") if existing else None
    data = apply_review(data, review)
    data["status"] = decide_status(status, data.get("impact"), policy, review, existing_status)
    data["run_id"] = run_id
    data["pack_version"] = pack_version
    return data


def rereview_item(existing: dict, policy, review) -> dict:
    """An item already in the store, re-decided by a review read this run."""
    data = dict(existing["data"])
    base_status = data.get("validated_status")
    if base_status not in INPUT_STATUSES:
        base_status = STATUS_DRAFT if data.get("status") in (STATUS_DRAFT, STATUS_PUBLISHED) else STATUS_REVIEW
    data = apply_review(data, review)
    data["status"] = decide_status(base_status, data.get("impact"), policy, review, existing["data"].get("status"))
    return data


def _record_deadlines(item: dict):
    """(docket_id, date) pairs for the comment close date of an item."""
    dockets = [d for d in item.get("docket_ids") or [] if isinstance(d, str) and d.strip()]
    close = item.get("comments_close_on")
    pairs = []
    if isinstance(close, str) and _ISO_DATE.match(close):
        pairs.extend((d, close) for d in dockets)
    for d in item.get("deadlines") or []:
        if isinstance(d, dict) and d.get("kind") == "comment_period_closes" and isinstance(d.get("date"), str) \
                and _ISO_DATE.match(d["date"]):
            dk = d.get("docket_id")
            for docket in ([dk] if isinstance(dk, str) and dk.strip() else dockets):
                pairs.append((docket, d["date"]))
    return pairs


def build_deadlines(items: dict, snapshot: Snapshot) -> dict:
    """{path: data}. Only items whose status is draft or published contribute."""
    by_docket = {}
    for iid, item in sorted(items.items()):
        if item.get("status") not in (STATUS_DRAFT, STATUS_PUBLISHED):
            continue
        for docket, date in _record_deadlines(item):
            by_docket.setdefault(segment(docket), {"docket_id": docket, "dates": set(), "items": set()})
            by_docket[segment(docket)]["dates"].add(date)
            by_docket[segment(docket)]["items"].add(iid)
    out = {}
    for key, d in sorted(by_docket.items()):
        path = f"deadlines/{key}"
        prev = snapshot.get(path)
        prev_items = prev["data"].get("item_ids") if prev else None
        ids = set(d["items"]) | {x for x in (prev_items or []) if isinstance(x, str)}
        out[path] = {
            "docket_id": d["docket_id"],
            "comments_close_on": max(d["dates"]),
            "docket_link": REGSGOV_DOCKET.format(d["docket_id"]),
            "item_ids": sorted(ids),
        }
    return out


def build_digest(day: str, run_items: dict, order: list, snapshot: Snapshot) -> tuple:
    path = f"digests/{day}"
    prev = snapshot.get(path)
    sections = {}
    if prev and isinstance(prev["data"].get("sections"), list):
        for s in prev["data"]["sections"]:
            if isinstance(s, dict) and isinstance(s.get("policy_area"), str) and isinstance(s.get("item_ids"), list):
                sections.setdefault(s["policy_area"], set()).update(x for x in s["item_ids"] if isinstance(x, str))
    statuses = {}
    reviewed = False
    for iid, item in run_items.items():
        area = item.get("policy_area") if isinstance(item.get("policy_area"), str) else "unclassified"
        for ids in sections.values():
            ids.discard(iid)
        sections.setdefault(area, set()).add(iid)
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
        reviewed = reviewed or "review" in item
    rank = {a: i for i, a in enumerate(order)}
    ordered = sorted((a for a in sections if sections[a]), key=lambda a: (rank.get(a, len(rank)), a))
    reviewed = reviewed or bool(prev and isinstance(prev["data"].get("label"), str)
                                and "reviewed by" in prev["data"]["label"])
    data = {
        "date": day,
        "label": f"{AI_LABEL}; reviewed by reviewer" if reviewed else f"{AI_LABEL}; not yet reviewed",
        "ai_assisted": True,
        "sections": [{"policy_area": a, "item_ids": sorted(sections[a])} for a in ordered],
        "item_ids": sorted({i for a in ordered for i in sections[a]}),
        "run_counts": dict(sorted(statuses.items())),
    }
    return path, data


def build_documents(records, snapshot=None, policy=None, run=None, order=None, allowed=None,
                    pack_version=None, now=None) -> dict:
    """Return {"writes": [...], "unchanged": [...], "counts": {...}, "run": run_doc}.

    Writes are ``{"op": "set", "path", "data"}`` plus ``if_version`` when the
    document already exists, in the order items, deadlines, digests, runs.
    """
    snapshot = snapshot if snapshot is not None else Snapshot()
    policy = policy if policy is not None else ReviewPolicy("advisory")
    run = dict(run or {})
    now = now or _now()
    run_id = segment(run.get("run_id") or now.replace(":", "").replace("-", ""))
    orch = run.get("orchestration", "session")
    if orch not in ORCHESTRATION_LEVELS:
        raise StoreError(f"orchestration must be one of {', '.join(ORCHESTRATION_LEVELS)}, got {orch!r}")
    started = run.get("started_at") if isinstance(run.get("started_at"), str) else now
    day = run.get("date") if isinstance(run.get("date"), str) and _ISO_DATE.match(run["date"]) else started[:10]
    if not _ISO_DATE.match(day):
        raise StoreError(f"the run date is not YYYY-MM-DD: {day!r}")

    reviews, review_ignored = collect_reviews(snapshot, allowed)
    docs = {}  # path -> data
    run_items = {}
    failures, missing_id = [], 0
    for rec in records:
        iid = item_id_of(rec)
        if iid is None:
            missing_id += 1
            iid = segment(f"unidentified-{run_id}-{missing_id}")
            rec = dict(rec, status=STATUS_REVIEW,
                       review_reason=rec.get("review_reason") or "schema_invalid: record has no id")
        path = f"items/{iid}"
        item = build_item(rec, run_id, pack_version, policy, reviews.get(iid), snapshot.get(path))
        if path in docs:  # a duplicate id in the input: keep both visible, never overwrite silently
            failures.append({"id": iid, "review_reason": "duplicate_id: a later record with this id was skipped"})
            continue
        docs[path] = item
        run_items[iid] = item
        if item.get("validated_status") == STATUS_REVIEW:
            failures.append({"id": iid, "review_reason": item.get("review_reason")})
    for iid, review in sorted(reviews.items()):
        path = f"items/{iid}"
        if path in docs:
            continue
        prev = snapshot.get(path)
        if prev is None:
            review_ignored.append((review["path"], "no item with this id in the store"))
            continue
        docs[path] = rereview_item(prev, policy, review)

    all_items = {p.split("/", 1)[1]: d for p, d in docs.items() if p.startswith("items/")}
    docs.update(build_deadlines(all_items, snapshot))
    dpath, digest = build_digest(day, run_items, order or [], snapshot)
    docs[dpath] = digest

    writes, unchanged = [], []
    for path in sorted(docs, key=lambda p: (WRITE_ORDER[p.split("/", 1)[0]], p)):
        prev = snapshot.get(path)
        data = docs[path]
        if prev is not None and _canon(prev["data"]) == _canon(data):
            unchanged.append(path)
            continue
        if prev is not None and path.startswith("items/") and data.get("run_id") == run_id \
                and _canon(dict(prev["data"], run_id=None)) == _canon(dict(data, run_id=None)):
            unchanged.append(path)
            continue
        w = {"op": "set", "path": path, "data": data}
        if prev is not None:
            w["if_version"] = prev["version"]
        writes.append(w)

    counts = {
        "records": len(records),
        "items": len(run_items),
        "items_rereviewed": len(all_items) - len(run_items),
        "by_status": {},
        "needs_review": sum(1 for i in run_items.values() if i["status"] == STATUS_REVIEW),
        "validator_failures": len(failures),
        "reviews_read": len(reviews),
        "rows_ignored": len(snapshot.ignored) + len(review_ignored),
        "writes": 0,
        "unchanged": len(unchanged),
    }
    for item in all_items.values():
        counts["by_status"][item["status"]] = counts["by_status"].get(item["status"], 0) + 1
    counts["by_status"] = dict(sorted(counts["by_status"].items()))

    run_path = f"runs/{run_id}"
    run_doc = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": run.get("finished_at") if isinstance(run.get("finished_at"), str) else now,
        "orchestration": orch,
        "sources_ok": list(run.get("sources_ok") or []),
        "sources_failed": list(run.get("sources_failed") or []),
        "review_mode": policy.describe(),
        "pack_version": pack_version,
        "digest": dpath,
        "counts": counts,
        "validator_failures": failures,
        "rows_ignored": [{"row": str(p), "why": w} for p, w in snapshot.ignored + review_ignored],
    }
    for k in ("config_version", "degraded", "notes"):
        if k in run:
            run_doc[k] = run[k]
    w = {"op": "set", "path": run_path, "data": run_doc}
    prev = snapshot.get(run_path)
    if prev is not None:
        w["if_version"] = prev["version"]
    writes.append(w)
    counts["writes"] = len(writes)
    return {"writes": writes, "unchanged": unchanged, "counts": counts, "run": run_doc, "run_id": run_id}


# ---------------------------------------------------------------------------
# batches


def _batch_obj(n, run_id, writes):
    return {"batch": n, "run_id": run_id, "writes": writes}


def batch_bytes(obj) -> int:
    return len(json_bytes(obj))


def json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def fit_write(write, run_id, max_bytes=MAX_BATCH_BYTES):
    """A write that fits in one batch on its own: shed bulky item fields if needed (recorded, never silent)."""
    if batch_bytes(_batch_obj(999999, run_id, [write])) <= max_bytes:
        return write
    if not write["path"].startswith("items/"):
        raise StoreError(f"{write['path']} is larger than one batch ({max_bytes} bytes)")
    data = dict(write["data"])
    shed = []
    for k in BULKY_FIELDS:
        if k in data:
            shed.append(k)
            del data[k]
            if batch_bytes(_batch_obj(999999, run_id, [dict(write, data=data)])) <= max_bytes:
                break
    data["shed_fields"] = shed
    if data.get("status") == STATUS_PUBLISHED or data.get("status") == STATUS_DRAFT:
        data["status"] = STATUS_REVIEW
    data["review_reason"] = "document_too_large: bulky fields were not stored"
    out = dict(write, data=data)
    if batch_bytes(_batch_obj(999999, run_id, [out])) > max_bytes:
        raise StoreError(f"{write['path']} is larger than one batch ({max_bytes} bytes) even without its bulky fields")
    return out


def plan_batches(writes, run_id, max_writes=MAX_WRITES_PER_BATCH, max_bytes=MAX_BATCH_BYTES) -> list:
    """Split writes, in order, into batches of at most max_writes and max_bytes (the file's bytes)."""
    batches, cur = [], []
    for w in writes:
        w = fit_write(w, run_id, max_bytes)
        trial = cur + [w]
        n = len(batches) + 1
        if cur and (len(trial) > max_writes or batch_bytes(_batch_obj(n, run_id, trial)) > max_bytes):
            batches.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        batches.append(cur)
    total = len(batches)
    return [dict(_batch_obj(i, run_id, b), of=total) for i, b in enumerate(batches, 1)]


# ---------------------------------------------------------------------------
# output


def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_previous(out_dir: pathlib.Path) -> None:
    """Remove only what this script wrote before: batch files, the manifest and the document copies."""
    if not out_dir.is_dir():
        return
    for p in out_dir.iterdir():
        if p.is_file() and (_BATCH_FILE.match(p.name) or p.name == "manifest.json"):
            p.unlink()
    for c in COLLECTIONS:
        d = out_dir / c
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.suffix == ".json":
                    p.unlink()


def write_store(out_dir, built: dict, batches: list) -> dict:
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous(out_dir)
    names = []
    for b in batches:
        name = f"batch-{b['batch']:03d}.json"
        payload = json_bytes(b)
        if len(payload) > MAX_BATCH_BYTES or len(b["writes"]) > MAX_WRITES_PER_BATCH:
            raise StoreError(f"{name} breaks the batch caps")  # plan_batches guarantees this
        _atomic_write(out_dir / name, payload)
        names.append(name)
        for w in b["writes"]:
            coll, rest = w["path"].split("/", 1)
            doc = {"path": w["path"], "data": w["data"]}
            if "if_version" in w:
                doc["if_version"] = w["if_version"]
            _atomic_write(out_dir / coll / f"{segment(rest)}.json",
                          (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    manifest = {
        "run_id": built["run_id"],
        "batches": names,
        "writes": sum(len(b["writes"]) for b in batches),
        "new": sum(1 for b in batches for w in b["writes"] if "if_version" not in w),
        "updates": sum(1 for b in batches for w in b["writes"] if "if_version" in w),
        "unchanged": built["unchanged"],
        "counts": built["counts"],
        "limits": {"max_writes_per_batch": MAX_WRITES_PER_BATCH, "max_batch_bytes": MAX_BATCH_BYTES},
    }
    _atomic_write(out_dir / "manifest.json",
                  (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return manifest


# ---------------------------------------------------------------------------
# CLI


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Build the artifact database documents for one run.")
    ap.add_argument("validated", help="validate.py output (JSON Lines)")
    ap.add_argument("--existing", help="rows read back from the database (JSON or JSON Lines)")
    ap.add_argument("--policies", default=str(DEFAULT_POLICIES))
    ap.add_argument("--pack", default=str(DEFAULT_PACK))
    ap.add_argument("--run", help="run facts (JSON): run_id, started_at, finished_at, orchestration, "
                                  "sources_ok, sources_failed, config_version")
    ap.add_argument("--run-id")
    ap.add_argument("--orchestration", choices=ORCHESTRATION_LEVELS)
    ap.add_argument("--date", help="digest date, YYYY-MM-DD (default: the run's start date, UTC)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        records = read_validated(args.validated)
        snapshot = read_existing(args.existing)
        policy = load_policy(args.policies)
        if not policy.known:
            log(f"store: review.mode {policy.raw!r} is not known; treating it as required_for_all")
        run = {}
        if args.run:
            try:
                run = json.loads(pathlib.Path(args.run).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise StoreError(f"cannot read run facts {args.run}: {e}") from e
            if not isinstance(run, dict):
                raise StoreError("run facts must be a JSON object")
        if args.run_id:
            run["run_id"] = args.run_id
        if args.orchestration:
            run["orchestration"] = args.orchestration
        if args.date:
            run["date"] = args.date
        order, allowed, pack_version = load_pack_info(args.pack)
        built = build_documents(records, snapshot, policy, run, order, allowed, pack_version)
        batches = plan_batches(built["writes"], built["run_id"])
        manifest = write_store(args.out, built, batches)
    except StoreError as e:
        log(f"store: {e}")
        return 2
    except OSError as e:
        log(f"store: cannot write the store: {e}")
        return 2
    summary = {"run_id": manifest["run_id"], "batches": len(manifest["batches"]), "writes": manifest["writes"],
               "new": manifest["new"], "updates": manifest["updates"], "unchanged": len(manifest["unchanged"]),
               "by_status": built["counts"]["by_status"], "out": str(args.out)}
    log(json.dumps(summary, sort_keys=True), stream=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
