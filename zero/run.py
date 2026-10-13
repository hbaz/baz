#!/usr/bin/env python3
"""The run's state machine (standard library only, Python 3.11).

    python3 zero/run.py [start] [--sim] [--executor auto|api|workflow|session]
                        [--out DIR] [--today YYYY-MM-DD] [--sources ID,ID]
                        [--existing FILE] [--known FILE] [--run-id ID]
                        [--policies FILE] [--pack DIR] [--batch-size N]
                        [--model ID] [--restart]
    python3 zero/run.py next   [--out DIR]
    python3 zero/run.py status [--out DIR]

``run.py`` owns the order of the steps, the retries, the checks and the run
record. The steps always run in this order, and nothing a model writes can
change it (an answer is data: it is checked against the step's schema, and
an answer that names a step, or carries any key the schema does not list,
is refused):

  1. fetch      every enabled source, retried on a whole-source failure;
  2. normalize  the raw records into items (a record that fails is an error
                entry for its source, never dropped);
  3. dedupe     against the store's item ids and earlier sources in the run;
                writes ``new_items.jsonl`` and ``run_summary.json``;
  4. sort       Claude: policy area, impact, stage, signals, citations, in
                batches (``--batch-size``, default 20; ``run.max_items`` caps
                how many items are sorted, the rest need review);
  5. assess     Claude: one step per item whose impact needs an assessment;
  6. validate   ``validate.py``'s checks on every record (schema, the pack's
                values, verbatim citations, dates and dockets);
  7. store      ``store.py``'s documents and batches in ``out/store/``, with
                the run record ``runs/{runId}`` written last.

The Claude steps (sort, assess) run through one of three executors. The
level used is the run's ``orchestration`` and is recorded in the run record:

  api       ``run.py`` calls the Messages API with structured outputs
            (``output_config.format`` with the step's JSON schema). Used by
            ``--executor auto`` when ``ANTHROPIC_API_KEY`` is set. Retryable
            HTTP failures (429, 5xx, network) are retried with backoff; when
            the API stays unreachable the run degrades to ``session`` and
            records ``degraded: true`` with the reason.
  workflow  ``run.py`` writes a workflow script (``out/run/workflow/
            stage-NNN.js``) generated from the pending steps: one
            schema-checked ``agent()`` call per step. The session runs it with
            the Workflow tool, saves what it returns to the answers file named
            in the output, and runs ``run.py next``.
  session   ``run.py`` writes one step file (``out/run/steps/step-NNN.json``)
            for the session's Claude: the instructions, the item data between
            the untrusted-content delimiters, the answer schema and the
            answer file to write. ``run.py next`` checks the answer before it
            advances. A refused answer is handed out again with the problems
            listed, at most ``MAX_ATTEMPTS`` times; then the step's items
            become ``needs_review`` (nothing is dropped).

State lives in ``out/run/state.json``; every write is atomic. Outputs:
``out/new_items.jsonl``, ``out/run_summary.json``, ``out/validated.jsonl``,
``out/store/`` (batches, documents, manifest) and ``out/run/run_record.json``.

The API key is read from the environment only, sent only in the
``x-api-key`` header to the API, and never written or logged.

Every command prints one JSON status line on stdout: ``state`` is
``complete``, ``waiting`` (a step file or workflow script is out; its path is
in ``step_file`` or ``workflow_script``) or ``failed``.

Exit status: 0 when the run is complete or waiting for an answer; 1 when the
run failed (a code step failed after its retries); 2 on bad usage, an
unreadable input, or no run to continue.
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
import time
import urllib.error
import urllib.request

ZERO = pathlib.Path(__file__).resolve().parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402
import scan  # noqa: E402
import store  # noqa: E402
import validate  # noqa: E402

REPO = ZERO.parent
DEFAULT_OUT = REPO / "out"
DEFAULT_PACK = ZERO / "pack"
DEFAULT_POLICIES = ZERO / "policies.yaml"

# The fixed step order. Nothing outside this module changes it.
STEPS = ("fetch", "normalize", "dedupe", "sort", "assess", "validate", "store")
CLAUDE_STEPS = ("sort", "assess")
CODE_STEPS = tuple(s for s in STEPS if s not in CLAUDE_STEPS)
EXECUTORS = ("api", "workflow", "session")  # best first; the orchestration levels
LEVEL_RANK = {e: i for i, e in enumerate(EXECUTORS)}

STATE_VERSION = 1
MAX_ATTEMPTS = 3          # per Claude step (answers refused before its items need review)
CODE_STEP_ATTEMPTS = 2    # per code step (an unexpected error is retried once)
FETCH_ATTEMPTS = 3        # per source, on a whole-source failure
FETCH_BACKOFF_S = 2.0
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_ITEMS = 300

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "ZERO_MODEL"
DEFAULT_MODEL = "claude-opus-5-5"
API_BETA = "server-side-fallback-2026-07-01"
API_MAX_TOKENS = 16000
API_EFFORT = "medium"
API_TIMEOUT_S = 600.0
API_ATTEMPTS = 4
API_BACKOFF_S = 2.0
API_RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)

ITEM_OPEN, ITEM_CLOSE = "<<<ITEM", "ITEM>>>"
ALLOWED_FLAGS = list(validate.ALLOWED_FLAGS)
# Item fields carried into a classified record (the model never supplies these).
ITEM_CORE = ("id", "source", "kind", "doc_id", "title", "url", "published", "docket_ids",
             "comments_close_on", "effective_on", "agencies", "provenance", "source_fixture")
# Item fields shown to the model.
ITEM_SHOWN = ("title", "source", "type", "agencies", "published", "comments_close_on",
              "effective_on", "docket_ids", "url")

# Seams for tests.
SLEEP = time.sleep


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UsageError(Exception):
    """Bad arguments, an unreadable input, or no run to continue."""


class StepFailed(Exception):
    """A code step failed after its retries."""


class ApiUnavailable(Exception):
    """The Messages API cannot be used for this run (after retries, or a non-retryable error)."""


class AnswerError(Exception):
    """One API answer could not be read (a refusal, a truncated or non-JSON reply)."""


# ---------------------------------------------------------------------------
# Files


def atomic_write(path, text: str) -> None:
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


def write_json(path, obj) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, rows) -> None:
    atomic_write(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows))


def read_jsonl(path) -> list:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _rel(path) -> str:
    p = pathlib.Path(path).resolve()
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# A small JSON Schema checker (the subset the step schemas use)


def check_schema(schema: dict, value, where="answer") -> list:
    """Problems with ``value`` against ``schema``: type, enum, properties,
    required, additionalProperties: false, items."""
    problems = []
    types = schema.get("type")
    if types is not None:
        tl = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in tl):
            return [f"{where} must be {' or '.join(tl)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} {value!r} is not one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in value:
                problems.append(f"{where} has no {k!r}")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    problems.append(f"{where} has a key the step does not ask for: {k!r}")
        for k, sub in props.items():
            if k in value:
                problems.extend(check_schema(sub, value[k], f"{where}.{k}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            problems.extend(check_schema(schema["items"], v, f"{where}[{i}]"))
    return problems


def _is_type(value, t) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return False


# ---------------------------------------------------------------------------
# The pack: values, prompts and the step schemas


def _enum_str(values) -> dict:
    return {"type": "string", "enum": list(values)} if values else {"type": "string"}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


class PackText:
    """The pack's values and prompt sections, as the step files use them."""

    def __init__(self, root=DEFAULT_PACK):
        self.root = pathlib.Path(root)
        self.pack = validate.Pack(self.root)  # raises validate.InputError
        self.taxonomy = validate._load(self.root / "taxonomy.yaml")
        self.impact = validate._load(self.root / "impact.yaml")
        wp = self.root / "watchlist.yaml"
        self.watchlist = validate._load(wp) if wp.exists() else {}
        try:
            text = (self.root / "prompts.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise validate.InputError(f"cannot read {self.root / 'prompts.md'}: {e}") from e
        self.sections = self._sections(text)
        for need in ("Rules for every step", "Step: sort", "Step: assess"):
            if need not in self.sections:
                raise validate.InputError(f"prompts.md has no section '## {need}'")

    @staticmethod
    def _sections(text: str) -> dict:
        out, name, buf = {}, None, []
        for line in text.splitlines():
            m = re.match(r"^## (.+?)\s*$", line)
            if m:
                if name is not None:
                    out[name] = "\n".join(buf).strip()
                name, buf = m.group(1), []
            elif name is not None:
                buf.append(line)
        if name is not None:
            out[name] = "\n".join(buf).strip()
        return out

    @property
    def rules(self) -> str:
        return "## Rules for every step\n\n" + self.sections["Rules for every step"]

    def _list(self, entries, fields) -> str:
        lines = []
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            parts = [str(e[f]) for f in fields if e.get(f)]
            lines.append(f"`{e['id']}`" + (": " + "; ".join(parts) if parts else ""))
        return "\n" + "\n".join(f"  - {ln}" for ln in lines) if lines else "(none)"

    def values(self) -> dict:
        watch = []
        for e in self.watchlist.get("agencies") or []:
            if isinstance(e, dict) and e.get("id"):
                watch.append(f"agency `{e['id']}`: {e.get('name', '')}")
        for e in self.watchlist.get("committees") or []:
            if isinstance(e, dict) and e.get("id"):
                pr = ", ".join(str(p) for p in e.get("priorities") or [])
                watch.append(f"committee `{e['id']}`: {e.get('label', '')}" + (f" (priorities: {pr})" if pr else ""))
        signals = self._list(self.impact.get("escalation_signals"), ("description",))
        rules = self._list(self.impact.get("rules"), ("description",))
        return {
            "policy_areas": self._list(self.taxonomy.get("policy_areas"), ("label", "description")),
            "stages": self._list(self.taxonomy.get("stages"), ("label",)),
            "impact_levels": self._list(self.impact.get("levels"), ("label", "definition")),
            "escalation_signals": signals + ("\n  Handling rules:" + rules if rules != "(none)" else ""),
            "watchlist": "\n" + "\n".join(f"  - {w}" for w in watch) if watch else "(none)",
            "deadline_kinds": ", ".join(f"`{k}`" for k in self.pack.deadline_kinds),
        }

    def area(self, area_id) -> str:
        for e in self.taxonomy.get("policy_areas") or []:
            if isinstance(e, dict) and e.get("id") == area_id:
                return f"`{area_id}`: {e.get('label', '')}; {e.get('description', '')}"
        return f"`{area_id}`"

    # schemas -------------------------------------------------------------

    def citation_schema(self, with_supports=True) -> dict:
        props = {"field": {"type": "string"}, "text": {"type": "string"}}
        if with_supports:
            props["supports"] = {"type": "string"}
        return _obj(props)

    def sort_schema(self) -> dict:
        p = self.pack
        rec = _obj({
            "item_id": {"type": "string"},
            "policy_area": _enum_str(p.policy_areas),
            "impact": _enum_str(p.impacts),
            "stage": _enum_str(p.stages),
            "reason": {"type": "string"},
            "signals": {"type": "array", "items": _enum_str(p.signals)},
            "committees": {"type": "array", "items": _enum_str(p.committees)},
            "flags": {"type": "array", "items": _enum_str(ALLOWED_FLAGS)},
            "citations": {"type": "array", "items": self.citation_schema()},
        })
        return _obj({"records": {"type": "array", "items": rec}})

    def assess_schema(self) -> dict:
        cit = self.citation_schema(with_supports=False)
        return _obj({
            "item_id": {"type": "string"},
            "summary": {"type": "string"},
            "changes_for_agencies": {"type": "array", "items": _obj({"point": {"type": "string"}, "citation": cit})},
            "deadlines": {"type": "array", "items": _obj({
                "kind": _enum_str(self.pack.deadline_kinds),
                "date": {"type": "string"},
                "docket_id": {"type": ["string", "null"]},
                "citation": cit,
            })},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "citations": {"type": "array", "items": self.citation_schema()},
        })

    # prompts ---------------------------------------------------------------

    @staticmethod
    def fill(template: str, slots: dict) -> str:
        def sub(m):
            k = m.group(1)
            return slots[k] if k in slots else m.group(0)
        return re.sub(r"\{\{(\w+)\}\}", sub, template)

    def sort_prompt(self, items) -> str:
        slots = dict(self.values(), item_count=str(len(items)), items="\n\n".join(item_block(i) for i in items))
        return self.fill(self.sections["Step: sort"], slots)

    def assess_prompt(self, item, sort_record) -> str:
        slots = dict(self.values(), item=item_block(item),
                     sort_record=json.dumps(sort_record, ensure_ascii=False, sort_keys=True),
                     policy_area=self.area(sort_record.get("policy_area")))
        return self.fill(self.sections["Step: assess"], slots)


def _defang(s: str) -> str:
    """Item text can never close or open the untrusted-content block."""
    return s.replace(ITEM_CLOSE, "ITEM> >>").replace(ITEM_OPEN, "<< <ITEM")


def item_block(item: dict) -> str:
    shown = {"item_id": item.get("id")}
    for k in ITEM_SHOWN:
        if item.get(k) not in (None, "", []):
            shown[k] = item[k]
    text = item.get("text") if isinstance(item.get("text"), dict) else {}
    shown["text"] = {k: v for k, v in text.items() if isinstance(v, str) and v.strip()}
    body = json.dumps(shown, ensure_ascii=False, indent=1, sort_keys=False)
    return f"{ITEM_OPEN}\n{_defang(body)}\n{ITEM_CLOSE}"


# ---------------------------------------------------------------------------
# The run


class Run:
    """One run: its state file and its outputs, all under ``out``."""

    def __init__(self, out_dir, state: dict):
        self.out = pathlib.Path(out_dir)
        self.state = state
        self._pack = None

    # paths
    @property
    def work(self):
        return self.out / "run"

    @staticmethod
    def state_path(out_dir):
        return pathlib.Path(out_dir) / "run" / "state.json"

    @property
    def items_path(self):
        return self.out / scan.ITEMS_FILE

    @property
    def summary_path(self):
        return self.out / scan.SUMMARY_FILE

    @property
    def validated_path(self):
        return self.out / "validated.jsonl"

    @property
    def store_dir(self):
        return self.out / "store"

    @property
    def opts(self) -> dict:
        return self.state["options"]

    @property
    def pack(self) -> PackText:
        if self._pack is None:
            self._pack = PackText(self.opts["pack"])
        return self._pack

    # state
    @classmethod
    def load(cls, out_dir):
        p = cls.state_path(out_dir)
        if not p.exists():
            raise UsageError(f"no run to continue in {out_dir} (start one with: python3 zero/run.py)")
        try:
            state = read_json(p)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise UsageError(f"cannot read the run state {p}: {e}") from e
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION \
                or [s.get("name") for s in state.get("steps") or []] != list(STEPS):
            raise UsageError(f"{p} is not a run state this run.py wrote")
        return cls(out_dir, state)

    def save(self):
        write_json(self.state_path(self.out), self.state)

    def step(self, name) -> dict:
        for s in self.state["steps"]:
            if s["name"] == name:
                return s
        raise KeyError(name)

    def current_step(self):
        """The first step that is not done: the only step that may run."""
        for s in self.state["steps"]:
            if s["status"] != "done":
                return s
        return None

    def note(self, text):
        self.state.setdefault("notes", []).append(text)

    def sleep(self, seconds):
        if not self.opts.get("sim"):
            SLEEP(seconds)

    def items(self) -> list:
        return read_jsonl(self.items_path)

    def tasks(self, step=None) -> list:
        return [t for t in self.state["tasks"] if step is None or t["step"] == step]

    def task(self, task_id):
        for t in self.state["tasks"]:
            if t["id"] == task_id:
                return t
        return None

    def answer_path(self, task_id):
        return self.work / "answers" / f"{store.segment(task_id)}.json"

    def accepted(self, task) -> dict:
        return read_json(self.answer_path(task["id"]))


def new_state(opts: dict, executor: str, run_id: str, started: str) -> dict:
    return {
        "version": STATE_VERSION,
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "status": "running",
        "executor_requested": opts.get("executor_requested", executor),
        "executor": executor,
        "orchestration": executor,
        "degraded": False,
        "notes": [],
        "options": opts,
        "steps": [{"name": s, "kind": "claude" if s in CLAUDE_STEPS else "code", "status": "pending",
                   "attempts": 0, "started_at": None, "finished_at": None, "error": None}
                  for s in STEPS],
        "tasks": [],
        "waiting": None,
        "seq": 0,
        "sources": {},
    }


# ---------------------------------------------------------------------------
# Code steps


def _source_entry(run: Run, sid) -> dict:
    return run.state["sources"].setdefault(sid, scan._entry(sid))


def _load_source(run: Run, sid):
    """(source dict or None). Records the reason on the entry when it is None."""
    entry = _source_entry(run, sid)
    path = pathlib.Path(run.opts["sources_dir"]) / f"{sid}.yaml"
    try:
        src = base.load_source_file(path)
    except (OSError, base.YamlError) as e:
        entry["status"] = "failed"
        entry["errors"].append(scan._error(sid, None, "load", f"cannot read {path.name}: {e}"))
        return None
    entry["kind"] = src.get("kind")
    if src.get("id") != sid:
        entry["status"] = "failed"
        entry["errors"].append(scan._error(sid, entry["kind"], "load", f"{path.name} declares id {src.get('id')!r}"))
        return None
    if src.get("enabled") is False:
        entry["status"] = "disabled"
        entry["reason"] = f"{path.name} has enabled: false" + (f" (status: {src['status']})" if src.get("status") else "")
        return None
    return src


def make_fetcher(run: Run):
    if run.opts.get("sim"):
        return base.PoliteFetcher(transport=base.FixtureTransport(run.opts["fixtures"]), sleep=lambda s: None)
    return base.PoliteFetcher()


def _window(run: Run):
    w = run.opts["window"]
    return base.Window.parse(w["start"], w["end"])


def step_fetch(run: Run, fetcher_factory=None):
    fetcher = (fetcher_factory or make_fetcher)(run)
    window = _window(run)
    run.state["sources"] = {}
    for sid in run.opts["source_ids"]:
        entry = _source_entry(run, sid)
        src = _load_source(run, sid)
        raw_path = run.work / "raw" / f"{store.segment(sid)}.json"
        if src is None:
            write_json(raw_path, {"source": sid, "records": [], "errors": [], "truncated": False})
            continue
        try:
            conn = base.create(src, fetcher=fetcher)
        except (KeyError, ValueError, TypeError) as e:
            entry["status"] = "failed"
            entry["errors"].append(scan._error(sid, entry["kind"], "load", e))
            write_json(raw_path, {"source": sid, "records": [], "errors": [], "truncated": False})
            continue
        records, errors, truncated, attempts, retried = [], [], False, 0, []
        while attempts < FETCH_ATTEMPTS:
            attempts += 1
            if errors:
                retried.extend(errors)
            try:
                batch = conn.fetch(window)
            except Exception as e:  # a whole-source failure (or a connector bug): retried, then recorded
                errors = [scan._error(sid, entry["kind"], "fetch", f"{type(e).__name__}: {e}")]
                records = []
            else:
                records, errors, truncated = list(batch), list(batch.errors), bool(batch.truncated)
                if records or not errors:
                    break
            if attempts < FETCH_ATTEMPTS:
                run.sleep(FETCH_BACKOFF_S * attempts)
        entry["fetch_attempts"] = attempts
        entry["retried_errors"] = retried  # earlier attempts' failures, kept visible
        entry["raw_count"] = len(records)
        entry["truncated"] = truncated
        entry["errors"].extend(errors)
        write_json(raw_path, {"source": sid, "records": records, "errors": errors, "truncated": truncated})


def step_normalize(run: Run):
    fetcher = base.PoliteFetcher()  # normalize never fetches; a connector just needs one to exist
    for sid in run.opts["source_ids"]:
        entry = _source_entry(run, sid)
        raw_path = run.work / "raw" / f"{store.segment(sid)}.json"
        out_path = run.work / "normalized" / f"{store.segment(sid)}.jsonl"
        if entry["status"] in ("failed", "disabled") and not entry["raw_count"]:
            write_jsonl(out_path, [])
            continue
        raw = read_json(raw_path)
        items = []
        src = base.load_source_file(pathlib.Path(run.opts["sources_dir"]) / f"{sid}.yaml")
        conn = base.create(src, fetcher=fetcher)
        for r in raw["records"]:
            try:
                item = conn.normalize_one(r)
            except (base.NormalizeError, KeyError, TypeError, ValueError) as e:
                ident = r.get("document_number") if isinstance(r, dict) else None
                entry["errors"].append(conn.error("normalize", ident, e))
                continue
            problems = scan.check_item(item)
            if problems:
                entry["invalid_count"] += 1
                ident = item.get("id") if isinstance(item, dict) else None
                entry["errors"].append(scan._error(sid, entry["kind"], "schema", "; ".join(problems), ident))
                continue
            items.append(item)
        entry["item_count"] = len(items)
        write_jsonl(out_path, items)


def step_dedupe(run: Run):
    known = set(run.opts.get("known_ids") or [])
    taken, new = set(), []
    scanned_at = run.state["started_at"]
    for sid in run.opts["source_ids"]:
        entry = _source_entry(run, sid)
        mine = []
        for item in read_jsonl(run.work / "normalized" / f"{store.segment(sid)}.jsonl"):
            iid = item["id"]
            if iid in known:
                entry["known_count"] += 1
            elif iid in taken:
                entry["duplicate_count"] += 1
            else:
                taken.add(iid)
                mine.append(dict(item, scanned_at=scanned_at))
        entry["new_count"] = len(mine)
        new.extend(mine)
        if entry["status"] != "disabled":
            if not entry["errors"]:
                entry["status"] = "ok"
            elif entry["raw_count"] or entry["item_count"]:
                entry["status"] = "partial"
            else:
                entry["status"] = "failed"
    entries = [run.state["sources"][s] for s in run.opts["source_ids"]]
    w = run.opts["window"]
    summary = {
        "scanned_at": scanned_at,
        "mode": "sim" if run.opts.get("sim") else "live",
        "window": {"start": w["start"], "end": w["end"],
                   "days": (_dt.date.fromisoformat(w["end"]) - _dt.date.fromisoformat(w["start"])).days + 1},
        "known_ids": len(known),
        "new_items": len(new),
        "sources_total": len(entries),
        "sources_by_status": {s: sum(1 for e in entries if e["status"] == s)
                              for s in ("ok", "partial", "failed", "disabled")},
        "failed_sources": [e["id"] for e in entries if e["status"] == "failed"],
        "sources": entries,
        "run_id": run.state["run_id"],
    }
    write_jsonl(run.items_path, new)
    write_json(run.summary_path, summary)


def merged_records(run: Run) -> list:
    """One record per new item, in order: item facts + the accepted sort and assess answers."""
    sorted_by_id, sort_fail = {}, {}
    for t in run.tasks("sort"):
        if t["status"] == "accepted":
            for r in run.accepted(t)["records"]:
                sorted_by_id[r["item_id"]] = r
        else:
            for iid in t["item_ids"]:
                sort_fail[iid] = t
    assessed, assess_fail = {}, {}
    for t in run.tasks("assess"):
        if t["status"] == "accepted":
            assessed[t["item_ids"][0]] = run.accepted(t)
        else:
            assess_fail[t["item_ids"][0]] = t
    unsorted = set(run.state.get("over_cap") or [])
    out = []
    for item in run.items():
        iid = item["id"]
        rec = {k: item[k] for k in ITEM_CORE if k in item}
        why = None
        if iid in sorted_by_id:
            s = sorted_by_id[iid]
            rec.update({k: v for k, v in s.items() if k != "item_id"})
            rec["citations"] = list(s.get("citations") or [])
            if iid in assessed:
                a = assessed[iid]
                for k in ("summary", "changes_for_agencies", "deadlines", "open_questions"):
                    if k in a:
                        rec[k] = a[k]
                rec["citations"] += list(a.get("citations") or [])
            elif iid in assess_fail:
                why = f"claude_step_failed: the assess answer was refused {assess_fail[iid]['attempts']} time(s)"
        elif iid in unsorted:
            why = f"not_sorted: over run.max_items ({run.opts['max_items']}) this run"
        elif iid in sort_fail:
            why = f"claude_step_failed: the sort answer was refused {sort_fail[iid]['attempts']} time(s)"
        else:
            why = "not_sorted: no sort answer for this item"
        rec["orchestration"] = run.state["orchestration"]
        out.append((rec, why))
    return out


def step_validate(run: Run):
    pack = validate.Pack(run.opts["pack"])
    items = {i["id"]: i for i in run.items()}
    pairs = merged_records(run)
    sources = validate.SourceIndex(items, run.opts["fixtures"])
    results = validate.validate_all([("record", r, None) for r, _ in pairs], pack, sources)
    for res, (_, why) in zip(results, pairs):
        if why:
            res["status"] = validate.STATUS_REVIEW
            res["review_reason"] = why + ("; " + res["review_reason"] if res.get("review_reason") else "")
            res["validation"]["result"] = validate.STATUS_REVIEW
            res["validation"]["reasons"] = [why.split(":", 1)[0]] + res["validation"]["reasons"]
    write_jsonl(run.validated_path, results)
    run.state["validation"] = validate.summarize(results)


def run_facts(run: Run) -> dict:
    src = run.state["sources"].values()
    facts = {
        "run_id": run.state["run_id"],
        "started_at": run.state["started_at"],
        "finished_at": _now(),
        "orchestration": run.state["orchestration"],
        "sources_ok": [e["id"] for e in src if e["status"] in ("ok", "partial")],
        "sources_failed": [e["id"] for e in src if e["status"] == "failed"],
        "date": run.opts["window"]["end"],
    }
    if run.state["degraded"] or run.state["notes"]:
        facts["degraded"] = bool(run.state["degraded"])
        facts["notes"] = list(run.state["notes"])
    return facts


def step_store(run: Run):
    records = store.read_validated(run.validated_path)
    snapshot = store.read_existing(run.opts.get("existing"))
    policy = store.load_policy(run.opts["policies"])
    if not policy.known:
        run.note(f"review.mode {policy.raw!r} is not known; treated as required_for_all")
    order, allowed, pack_version = store.load_pack_info(run.opts["pack"])
    built = store.build_documents(records, snapshot, policy, run_facts(run), order, allowed, pack_version)
    batches = store.plan_batches(built["writes"], built["run_id"])
    manifest = store.write_store(run.store_dir, built, batches)
    record = dict(built["run"], executor_requested=run.state["executor_requested"],
                  steps=[{k: s[k] for k in ("name", "kind", "status", "attempts", "started_at", "finished_at")}
                         for s in run.state["steps"]],
                  claude_steps=[{k: t[k] for k in ("id", "step", "status", "attempts", "executor")}
                                for t in run.state["tasks"]],
                  batches=manifest["batches"])
    write_json(run.work / "run_record.json", record)
    run.state["store"] = {"batches": manifest["batches"], "writes": manifest["writes"], "out": _rel(run.store_dir)}


CODE_STEP_FUNCS = {"fetch": step_fetch, "normalize": step_normalize, "dedupe": step_dedupe,
                   "validate": step_validate, "store": step_store}


# ---------------------------------------------------------------------------
# Claude steps: planning and checking answers


def plan_tasks(run: Run, step_name: str):
    if run.tasks(step_name):
        return
    if step_name == "sort":
        items = run.items()
        cap = run.opts["max_items"]
        run.state["over_cap"] = [i["id"] for i in items[cap:]]
        if run.state["over_cap"]:
            run.note(f"{len(run.state['over_cap'])} item(s) over run.max_items ({cap}) were not sorted; they need review")
        items = items[:cap]
        size = run.opts["batch_size"]
        for n in range(0, len(items), size):
            ids = [i["id"] for i in items[n:n + size]]
            _add_task(run, "sort", ids)
    elif step_name == "assess":
        need = run.pack.pack.requires_assessment
        for t in run.tasks("sort"):
            if t["status"] != "accepted":
                continue
            for r in run.accepted(t)["records"]:
                if r.get("impact") in need:
                    _add_task(run, "assess", [r["item_id"]])


def _add_task(run, step, ids):
    n = len(run.tasks(step)) + 1
    run.state["tasks"].append({"id": f"{step}-{n:03d}", "step": step, "item_ids": ids, "status": "pending",
                               "attempts": 0, "problems": [], "executor": None})


def task_prompt(run: Run, task) -> str:
    items = {i["id"]: i for i in run.items()}
    if task["step"] == "sort":
        prompt = run.pack.sort_prompt([items[i] for i in task["item_ids"]])
    else:
        iid = task["item_ids"][0]
        sort_rec = None
        for t in run.tasks("sort"):
            if t["status"] == "accepted":
                for r in run.accepted(t)["records"]:
                    if r["item_id"] == iid:
                        sort_rec = r
        prompt = run.pack.assess_prompt(items[iid], sort_rec or {"item_id": iid})
    if task["problems"]:
        prompt += ("\n\n## Your previous answer was refused\n\nFix these problems and answer again "
                   "with the whole answer:\n\n" + "\n".join(f"- {p}" for p in task["problems"]))
    return prompt


def task_schema(run: Run, task) -> dict:
    return run.pack.sort_schema() if task["step"] == "sort" else run.pack.assess_schema()


def check_answer(run: Run, task, answer) -> list:
    """Problems with an answer. An empty list means the answer is accepted."""
    problems = check_schema(task_schema(run, task), answer)
    if problems:
        return problems
    if task["step"] == "sort":
        got = [r["item_id"] for r in answer["records"]]
        want = task["item_ids"]
        for iid in sorted(set(want) - set(got)):
            problems.append(f"no record for item {iid!r}")
        for iid in sorted(set(got) - set(want)):
            problems.append(f"record for {iid!r}, which is not in this step")
        dup = sorted({i for i in got if got.count(i) > 1})
        for iid in dup:
            problems.append(f"more than one record for item {iid!r}")
        for i, r in enumerate(answer["records"]):
            if not r["citations"]:
                problems.append(f"records[{i}] has no citation")
            if not r["reason"].strip():
                problems.append(f"records[{i}] has an empty reason")
    else:
        if answer["item_id"] != task["item_ids"][0]:
            problems.append(f"item_id must be {task['item_ids'][0]!r}, got {answer['item_id']!r}")
        if not answer["summary"].strip():
            problems.append("summary is empty")
    return problems


def take_answer(run: Run, task, answer, executor: str) -> bool:
    """Check one answer; accept it, or count the attempt. Returns True when accepted."""
    task["attempts"] += 1
    task["executor"] = executor
    problems = check_answer(run, task, answer)
    if not problems:
        write_json(run.answer_path(task["id"]), answer)
        task["status"] = "accepted"
        task["problems"] = []
        return True
    task["problems"] = problems[:20]
    if task["attempts"] >= MAX_ATTEMPTS:
        task["status"] = "failed"
        run.note(f"{task['id']}: answer refused {task['attempts']} time(s); its items need review")
    return False


def lower_level(run: Run, level: str, why: str):
    """Record that a Claude step ran at ``level`` (the run's orchestration is the lowest level used)."""
    if LEVEL_RANK[level] > LEVEL_RANK[run.state["orchestration"]]:
        run.state["orchestration"] = level
        run.state["degraded"] = True
        run.note(why)


# ---------------------------------------------------------------------------
# Executors


class SessionExecutor:
    """Level 3: one step file at a time for the session's Claude."""

    name = "session"

    def drive(self, run: Run, step_name: str, pending: list) -> str:
        task = pending[0]
        run.state["seq"] += 1
        n = run.state["seq"]
        steps_dir = run.work / "steps"
        step_file = steps_dir / f"step-{n:03d}.json"
        answer_file = steps_dir / f"step-{n:03d}.answer.json"
        if answer_file.exists():
            answer_file.unlink()
        doc = {
            "run_id": run.state["run_id"],
            "step": step_name,
            "task_id": task["id"],
            "attempt": task["attempts"] + 1,
            "max_attempts": MAX_ATTEMPTS,
            "what_to_do": (f"Read 'rules' and 'instructions'. Write ONLY the JSON answer, matching "
                           f"'answer_schema', to {_rel(answer_file)}. Then run: python3 zero/run.py next "
                           f"(it checks the answer before the run goes on; you never choose the next step)."),
            "rules": run.pack.rules,
            "instructions": task_prompt(run, task),
            "answer_schema": task_schema(run, task),
            "answer_file": _rel(answer_file),
            "previous_problems": list(task["problems"]),
            "then": "python3 zero/run.py next",
        }
        write_json(step_file, doc)
        run.state["waiting"] = {"executor": "session", "task_ids": [task["id"]], "step": step_name,
                                "step_file": str(step_file), "answer_file": str(answer_file)}
        return "waiting"

    def intake(self, run: Run, waiting: dict) -> bool:
        path = pathlib.Path(waiting["answer_file"])
        if not path.exists():
            return False
        task = run.task(waiting["task_ids"][0])
        try:
            answer = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            answer = None
            task["attempts"] += 1
            task["executor"] = self.name
            task["problems"] = [f"the answer file is not JSON: {e}"]
            if task["attempts"] >= MAX_ATTEMPTS:
                task["status"] = "failed"
                run.note(f"{task['id']}: answer refused {task['attempts']} time(s); its items need review")
        else:
            take_answer(run, task, answer, self.name)
        lower_level(run, self.name, f"{task['id']} ran in the session")
        return True


class WorkflowExecutor:
    """Level 2: a workflow script with one schema-checked agent() call per pending step."""

    name = "workflow"

    def drive(self, run: Run, step_name: str, pending: list) -> str:
        run.state["seq"] += 1
        n = run.state["seq"]
        wdir = run.work / "workflow"
        script = wdir / f"stage-{n:03d}.js"
        answers = wdir / f"stage-{n:03d}.answers.json"
        if answers.exists():
            answers.unlink()
        tasks = [{"id": t["id"], "label": f"{step_name} {t['id']}",
                  "prompt": run.pack.rules + "\n\n" + task_prompt(run, t) +
                  "\n\nReturn the answer through the StructuredOutput tool only.",
                  "schema": task_schema(run, t)} for t in pending]
        write_text = emit_workflow_script(run.state["run_id"], step_name, n, tasks)
        atomic_write(script, write_text)
        run.state["waiting"] = {"executor": "workflow", "task_ids": [t["id"] for t in pending], "step": step_name,
                                "workflow_script": str(script), "answer_file": str(answers)}
        return "waiting"

    def intake(self, run: Run, waiting: dict) -> bool:
        path = pathlib.Path(waiting["answer_file"])
        if not path.exists():
            return False
        try:
            doc = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            doc = {}
            run.note(f"workflow answers file {path.name} is not JSON: {e}")
        answers = doc.get("answers") if isinstance(doc, dict) and isinstance(doc.get("answers"), dict) else {}
        extra = sorted(set(answers) - set(waiting["task_ids"]))
        if extra:
            run.note(f"workflow answers for steps not handed out were ignored: {', '.join(extra)}")
        for tid in waiting["task_ids"]:
            task = run.task(tid)
            if tid in answers and answers[tid] is not None:
                take_answer(run, task, answers[tid], self.name)
            else:
                task["attempts"] += 1
                task["executor"] = self.name
                task["problems"] = ["the workflow returned no answer for this step"]
                if task["attempts"] >= MAX_ATTEMPTS:
                    task["status"] = "failed"
                    run.note(f"{tid}: no answer after {task['attempts']} attempt(s); its items need review")
        lower_level(run, self.name, f"{waiting['step']} ran in a workflow")
        return True


def emit_workflow_script(run_id: str, step_name: str, n: int, tasks: list) -> str:
    """A Workflow tool script generated from the step list. The script holds
    no control flow beyond running every handed-out step once; run.py checks
    what it returns and decides what comes next."""
    meta = {
        "name": f"zero-run-{store.segment(run_id)}-stage-{n:03d}",
        "description": f"RegWatch run {run_id}: the {step_name} step ({len(tasks)} call(s)), answers checked by run.py",
        "phases": [{"title": step_name, "detail": f"{len(tasks)} schema-checked call(s)"}],
    }
    # ensure_ascii keeps U+2028/U+2029 and every non-ASCII character escaped, so the literal is valid JS.
    lines = [
        "export const meta = " + json.dumps(meta, ensure_ascii=True, indent=2),
        "// Generated by zero/run.py from its fixed step list. Do not edit: run.py checks every answer",
        "// against the same schema and decides the next step itself.",
        "const TASKS = " + json.dumps(tasks, ensure_ascii=True),
        f"phase({json.dumps(step_name)})",
        "const results = await parallel(TASKS.map(t => () => agent(t.prompt, "
        f"{{label: t.label, phase: {json.dumps(step_name)}, schema: t.schema}})))",
        "const answers = {}",
        "TASKS.forEach((t, i) => { answers[t.id] = results[i] === undefined ? null : results[i] })",
        f"log('{len(tasks)} {step_name} answer(s) returned; save this result and run python3 zero/run.py next')",
        f"return {{run_id: {json.dumps(run_id)}, stage: {n}, answers}}",
        "",
    ]
    return "\n".join(lines)


def urllib_post(url: str, headers: dict, body: bytes, timeout: float):
    """(status, headers, body bytes). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() if e.fp else b""


class ApiExecutor:
    """Level 1: the Messages API with structured outputs, one call per step."""

    name = "api"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, transport=None, sleep=None):
        if not api_key:
            raise ApiUnavailable(f"{API_KEY_ENV} is not set")
        self._key = api_key
        self.model = model
        self.transport = transport or urllib_post
        self.sleep = sleep or SLEEP

    def request_body(self, run: Run, task) -> dict:
        return {
            "model": self.model,
            "max_tokens": API_MAX_TOKENS,
            "system": run.pack.rules,
            "messages": [{"role": "user", "content": task_prompt(run, task)}],
            "output_config": {"effort": API_EFFORT,
                              "format": {"type": "json_schema", "schema": task_schema(run, task)}},
            "fallbacks": "default",
        }

    def _headers(self) -> dict:
        return {"content-type": "application/json", "x-api-key": self._key,
                "anthropic-version": API_VERSION, "anthropic-beta": API_BETA}

    def call(self, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = None
        for attempt in range(1, API_ATTEMPTS + 1):
            try:
                status, headers, raw = self.transport(API_URL, self._headers(), data, API_TIMEOUT_S)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, headers, raw, last = None, {}, b"", f"network error: {type(e).__name__}"
            if status == 200:
                return self.parse(raw)
            if status is not None:
                last = f"HTTP {status}: {_api_error_text(raw)}"
                if status not in API_RETRY_STATUS:
                    raise ApiUnavailable(last)
            if attempt < API_ATTEMPTS:
                self.sleep(_retry_after(headers) or API_BACKOFF_S * (2 ** (attempt - 1)))
        raise ApiUnavailable(f"the Messages API failed {API_ATTEMPTS} times; last: {last}")

    @staticmethod
    def parse(raw: bytes):
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AnswerError(f"the API reply is not JSON: {e}") from e
        stop = msg.get("stop_reason") if isinstance(msg, dict) else None
        if stop == "refusal":
            raise AnswerError("the model declined the request (stop_reason: refusal)")
        if stop == "max_tokens":
            raise AnswerError("the answer was cut off (stop_reason: max_tokens)")
        text = "".join(b.get("text", "") for b in (msg.get("content") or [])
                       if isinstance(b, dict) and b.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise AnswerError(f"the answer is not JSON: {e.msg}") from e

    def drive(self, run: Run, step_name: str, pending: list) -> str:
        for task in pending:
            while task["status"] == "pending":
                try:
                    answer = self.call(self.request_body(run, task))
                except AnswerError as e:
                    task["attempts"] += 1
                    task["executor"] = self.name
                    task["problems"] = [str(e)]
                    if task["attempts"] >= MAX_ATTEMPTS:
                        task["status"] = "failed"
                        run.note(f"{task['id']}: {e} ({task['attempts']} attempt(s)); its items need review")
                    run.save()
                    continue
                take_answer(run, task, answer, self.name)
                run.save()
        return "done"


def _api_error_text(raw: bytes) -> str:
    try:
        err = json.loads(raw.decode("utf-8")).get("error") or {}
        return str(err.get("type") or "error") + (": " + str(err.get("message"))[:200] if err.get("message") else "")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return "no error body"


def _retry_after(headers) -> float:
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return min(float(v), 60.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def make_executor(run: Run, transport=None):
    name = run.state["executor"]
    if name == "api":
        return ApiExecutor(os.environ.get(API_KEY_ENV, ""), run.opts.get("model") or DEFAULT_MODEL,
                           transport=transport)
    if name == "workflow":
        return WorkflowExecutor()
    return SessionExecutor()


# ---------------------------------------------------------------------------
# The state machine


def advance(run: Run, transport=None, fetcher_factory=None) -> str:
    """Run every step that can run now, in the fixed order. Returns the run's state."""
    while True:
        step = run.current_step()
        if step is None:
            run.state["status"] = "complete"
            run.state["finished_at"] = run.state["finished_at"] or _now()
            run.save()
            return "complete"
        if step["status"] == "failed":
            return "failed"
        if step["started_at"] is None:
            step["started_at"] = _now()
        if step["kind"] == "code":
            fn = CODE_STEP_FUNCS[step["name"]]
            while True:
                step["attempts"] += 1
                try:
                    if step["name"] == "fetch":
                        fn(run, fetcher_factory)
                    else:
                        fn(run)
                except Exception as e:  # recorded on the step and retried; never swallowed
                    step["error"] = f"{type(e).__name__}: {store.redact(str(e))}"
                    if step["attempts"] >= CODE_STEP_ATTEMPTS:
                        step["status"] = "failed"
                        run.state["status"] = "failed"
                        run.note(f"step {step['name']} failed after {step['attempts']} attempt(s): {step['error']}")
                        run.save()
                        return "failed"
                    continue
                break
            step["status"] = "done"
            step["error"] = None
            step["finished_at"] = _now()
            run.save()
            continue
        # a Claude step
        if run.state.get("waiting"):
            return "waiting"
        plan_tasks(run, step["name"])
        pending = [t for t in run.tasks(step["name"]) if t["status"] == "pending"]
        if not pending:
            step["status"] = "done"
            step["attempts"] = sum(t["attempts"] for t in run.tasks(step["name"]))
            step["finished_at"] = _now()
            run.save()
            continue
        try:
            executor = make_executor(run, transport)
            result = executor.drive(run, step["name"], pending)
        except ApiUnavailable as e:
            run.state["executor"] = "session"
            lower_level(run, "session", f"the API level was not available ({store.redact(str(e))}); "
                                        "the Claude steps fall back to the session")
            run.save()
            continue
        run.save()
        if result == "waiting":
            return "waiting"


def intake(run: Run) -> bool:
    """Read the answer(s) to what was handed out. False when nothing is there yet."""
    waiting = run.state.get("waiting")
    if not waiting:
        return True
    executor = WorkflowExecutor() if waiting["executor"] == "workflow" else SessionExecutor()
    if not executor.intake(run, waiting):
        return False
    run.state["waiting"] = None
    run.save()
    return True


def status_line(run: Run, state: str) -> dict:
    cur = run.current_step()
    out = {"state": state, "run_id": run.state["run_id"], "orchestration": run.state["orchestration"],
           "step": cur["name"] if cur else None}
    w = run.state.get("waiting")
    if state == "waiting" and w:
        out["executor"] = w["executor"]
        if w.get("step_file"):
            out["step_file"] = _rel(w["step_file"])
        if w.get("workflow_script"):
            out["workflow_script"] = _rel(w["workflow_script"])
        out["answer_file"] = _rel(w["answer_file"])
        out["then"] = "python3 zero/run.py next"
    if state == "complete":
        out["store"] = run.state.get("store")
        out["validation"] = run.state.get("validation")
        out["degraded"] = run.state["degraded"]
    if state == "failed":
        out["notes"] = run.state["notes"][-3:]
    return out


# ---------------------------------------------------------------------------
# Starting a run


def choose_executor(requested: str, environ=None) -> str:
    environ = os.environ if environ is None else environ
    if requested == "auto":
        return "api" if environ.get(API_KEY_ENV) else "session"
    if requested not in EXECUTORS:
        raise UsageError(f"--executor must be auto, {', '.join(EXECUTORS)}")
    return requested


def build_options(args) -> dict:
    policies = scan.load_policies(args.policies)
    ids = ([s.strip() for s in args.sources.split(",") if s.strip()]
           if args.sources is not None else scan.enabled_ids(policies))
    if not ids:
        raise UsageError("no sources to scan")
    days = scan.lookback_days(policies)
    if args.today:
        try:
            today = _dt.date.fromisoformat(args.today)
        except ValueError as e:
            raise UsageError(f"--today is not YYYY-MM-DD: {args.today!r}") from e
    elif args.sim:
        today = scan.latest_fixture_date(args.fixtures)
        if today is None:
            raise UsageError(f"no dated fixtures under {args.fixtures}")
    else:
        today = _dt.date.today()
    window = base.Window.lookback(days, today)
    max_items = (policies.get("run") or {}).get("max_items", DEFAULT_MAX_ITEMS)
    try:
        max_items = int(max_items)
    except (TypeError, ValueError) as e:
        raise UsageError(f"run.max_items is not a whole number: {max_items!r}") from e
    if max_items < 1 or args.batch_size < 1:
        raise UsageError("run.max_items and --batch-size must be at least 1")
    known = set(scan.load_known_ids(args.known))
    existing = None
    if args.existing:
        try:
            snap = store.read_existing(args.existing)
        except store.StoreError as e:
            raise UsageError(str(e)) from e
        known |= {p.split("/", 1)[1] for p in snap.under("items")}
        existing = str(pathlib.Path(args.existing).resolve())
    try:
        PackText(args.pack)
    except validate.InputError as e:
        raise UsageError(str(e)) from e
    return {
        "sim": bool(args.sim),
        "source_ids": ids,
        "sources_dir": str(pathlib.Path(args.sources_dir).resolve()),
        "fixtures": str(pathlib.Path(args.fixtures).resolve()),
        "policies": str(pathlib.Path(args.policies).resolve()),
        "pack": str(pathlib.Path(args.pack).resolve()),
        "existing": existing,
        "known_ids": sorted(known),
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "max_items": max_items,
        "batch_size": args.batch_size,
        "model": args.model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        "executor_requested": args.executor,
    }


def start(args, transport=None, fetcher_factory=None):
    out = pathlib.Path(args.out)
    sp = Run.state_path(out)
    if sp.exists() and not args.restart:
        try:
            prev = read_json(sp)
            unfinished = prev.get("status") == "running"
        except (OSError, ValueError):
            unfinished = True
        if unfinished:
            raise UsageError(f"an unfinished run is in {out}: continue it with 'python3 zero/run.py next', "
                             "or start over with --restart")
    opts = build_options(args)
    executor = choose_executor(args.executor)
    started = _now()
    run_id = store.segment(args.run_id or started.replace(":", "").replace("-", ""))
    if (out / "run").is_dir():
        for sub in ("raw", "normalized", "answers", "steps", "workflow"):
            d = out / "run" / sub
            if d.is_dir():
                for p in d.iterdir():
                    if p.is_file():
                        p.unlink()
    run = Run(out, new_state(opts, executor, run_id, started))
    if args.executor == "auto" and executor == "session":
        run.note(f"{API_KEY_ENV} is not set; the Claude steps run in the session")
    run.save()
    return run, advance(run, transport=transport, fetcher_factory=fetcher_factory)


def cont(args, transport=None, fetcher_factory=None):
    run = Run.load(args.out)
    if run.state["status"] in ("complete", "failed"):
        return run, run.state["status"]
    if not intake(run):
        return run, "waiting"
    return run, advance(run, transport=transport, fetcher_factory=fetcher_factory)


# ---------------------------------------------------------------------------
# CLI


def parse_args(argv):
    p = argparse.ArgumentParser(prog="run.py", description="Run the steps in their fixed order.")
    p.add_argument("command", nargs="?", default="start", choices=("start", "next", "status"),
                   help="start a run (default), continue it (next), or show it (status)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: out/)")
    p.add_argument("--sim", action="store_true", help="replay zero/fixtures/ instead of the network")
    p.add_argument("--executor", default="auto", choices=("auto",) + EXECUTORS,
                   help="how the Claude steps run (auto: api when ANTHROPIC_API_KEY is set, else session)")
    p.add_argument("--today", help="last day of the window, YYYY-MM-DD")
    p.add_argument("--sources", help="comma-separated source ids (override sources.enabled)")
    p.add_argument("--existing", help="rows read back from the artifact database (for store.py)")
    p.add_argument("--known", help="file of known item ids")
    p.add_argument("--run-id")
    p.add_argument("--policies", default=str(DEFAULT_POLICIES))
    p.add_argument("--pack", default=str(DEFAULT_PACK))
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="items per sort step")
    p.add_argument("--model", help=f"model for the api executor (default: ${MODEL_ENV} or {DEFAULT_MODEL})")
    p.add_argument("--restart", action="store_true", help="start over even if a run is unfinished")
    p.add_argument("--fixtures", default=str(base.FIXTURES_ROOT), help=argparse.SUPPRESS)
    p.add_argument("--sources-dir", default=str(scan.SOURCES_DIR), help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None, transport=None, fetcher_factory=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "start":
            run, state = start(args, transport, fetcher_factory)
        elif args.command == "next":
            run, state = cont(args, transport, fetcher_factory)
        else:
            run = Run.load(args.out)
            state = run.state["status"] if run.state["status"] != "running" else (
                "waiting" if run.state.get("waiting") else "running")
    except (UsageError, scan.UsageError) as e:
        print(f"run: {store.redact(str(e))}", file=sys.stderr)
        return 2
    line = status_line(run, state)
    print(json.dumps(line, sort_keys=True))
    if state == "waiting":
        what = line.get("step_file") or line.get("workflow_script")
        print(f"run: waiting on {what}; write the answer to {line['answer_file']}, "
              "then run: python3 zero/run.py next", file=sys.stderr)
    elif state == "complete":
        print(f"run: complete ({run.state['orchestration']}); store batches in {_rel(run.store_dir)}",
              file=sys.stderr)
    return 1 if state == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
