"""Every fixture under zero/fixtures/ declares where it came from.

A fixture declares ``provenance: recorded`` (a captured real response) or
``provenance: synthetic`` (invented data in the real shape):
  - .json:  a top-level ``"provenance"`` key (a top-level list: on every element);
  - .jsonl: a ``"provenance"`` key on every record;
  - any other file (HTML, XML, RSS, YAML, text): a ``provenance: <value>``
    line, for example inside a comment.

A ``recorded`` fixture may only come from a source whose definition says
``names_people: false``. When the source names people, or the flag is
absent, or the source cannot be found, a recorded fixture fails: such a
response must be synthesized with invented names instead.

The fixture's source is its ``"source"`` key (JSON), a ``source: <id>``
line (other files), or else the first folder under zero/fixtures/.
Standard library only.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest

ZERO = pathlib.Path(__file__).resolve().parent.parent
if str(ZERO) not in sys.path:
    sys.path.insert(0, str(ZERO))

from sources import base  # noqa: E402

FIXTURES = ZERO / "fixtures"
SOURCE_DIRS = [ZERO / "pack" / "sources", ZERO / "packs"]
ALLOWED = ("recorded", "synthetic")
_PROV_LINE = re.compile(r"""["']?provenance["']?\s*:\s*["']?([A-Za-z_]+)""")
_SOURCE_LINE = re.compile(r"""(?m)^\W*["']?source["']?\s*:\s*["']?([a-z0-9_\-]+)""")


def source_flags(dirs=SOURCE_DIRS) -> dict:
    """Map source id -> its ``names_people`` value (None when absent)."""
    out = {}
    for d in dirs:
        d = pathlib.Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.yaml")):
            if d.name == "packs" and "sources" not in p.relative_to(d).parts:
                continue
            try:
                src = base.load_source_file(p)
            except (ValueError, OSError):
                continue
            sid = src.get("id")
            if isinstance(sid, str) and sid not in out:
                out[sid] = src.get("names_people")
    return out


def fixture_files(root=FIXTURES):
    root = pathlib.Path(root)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            yield p


def declarations(path: pathlib.Path, root=FIXTURES):
    """Yield (where, provenance or None, source id or None) for each declaring unit in a fixture."""
    rel = path.relative_to(root)
    folder_source = rel.parts[0] if len(rel.parts) > 1 else None
    text = path.read_text(encoding="utf-8", errors="replace")

    def unit(where, rec):
        if not isinstance(rec, dict):
            return (where, None, folder_source)
        return (where, rec.get("provenance"), rec.get("source") or folder_source)

    if path.suffix == ".json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            yield (f"{rel}: invalid JSON ({e})", None, folder_source)
            return
        if isinstance(doc, list):
            if not doc:
                yield (f"{rel}: empty list", None, folder_source)
            for i, rec in enumerate(doc):
                yield unit(f"{rel}[{i}]", rec)
        else:
            yield unit(str(rel), doc)
    elif path.suffix == ".jsonl":
        n = 0
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            n += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                yield (f"{rel}:{i}: invalid JSON ({e})", None, folder_source)
                continue
            yield unit(f"{rel}:{i}", rec)
        if n == 0:
            yield (f"{rel}: no records", None, folder_source)
    else:
        m = _PROV_LINE.search(text)
        s = _SOURCE_LINE.search(text)
        yield (str(rel), m.group(1) if m else None, (s.group(1) if s else None) or folder_source)


def problems(root=FIXTURES, flags=None) -> list:
    root = pathlib.Path(root)
    flags = source_flags() if flags is None else flags
    out = []
    files = list(fixture_files(root))
    if not files:
        out.append(f"no fixtures under {root}")
    for p in files:
        for where, prov, sid in declarations(p, root):
            if prov not in ALLOWED:
                out.append(f"{where}: provenance must be one of {ALLOWED}, got {prov!r}")
            elif prov == "recorded":
                if sid not in flags:
                    out.append(f"{where}: recorded fixture from unknown source {sid!r} (names_people absent)")
                elif flags[sid] is not False:
                    out.append(f"{where}: recorded fixture from source {sid!r} with names_people "
                               f"{flags[sid]!r}; synthesize it with invented names")
    return out


class FixtureProvenanceTests(unittest.TestCase):
    def test_every_fixture_declares_provenance(self):
        self.assertEqual(problems(), [])

    def test_rule_catches_bad_fixtures(self):
        flags = {"names": True, "absent": None, "clean": False}
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)

            def write(rel, content):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")

            write("clean/a.json", json.dumps({"provenance": "recorded", "source": "clean"}))
            write("names/b.json", json.dumps({"provenance": "synthetic"}))
            self.assertEqual(problems(root, flags), [])

            write("names/c.json", json.dumps({"provenance": "recorded"}))
            write("absent/d.html", "<!-- provenance: recorded -->\n<html></html>")
            write("e.jsonl", json.dumps({"provenance": "recorded", "source": "nowhere"}) + "\n")
            write("clean/f.json", json.dumps({"source": "clean"}))
            write("clean/g.xml", "<rss></rss>")
            write("clean/h.jsonl", json.dumps({"provenance": "made-up"}) + "\n")
            found = problems(root, flags)
            for name in ("c.json", "d.html", "e.jsonl", "f.json", "g.xml", "h.jsonl"):
                self.assertTrue(any(name in f for f in found), (name, found))
            self.assertEqual(len(found), 6, found)
            self.assertFalse(any("a.json" in f or "b.json" in f for f in found))

    def test_pack_sources_resolve(self):
        flags = source_flags()
        self.assertIs(flags.get("fedreg"), True)
        self.assertIs(flags.get("fedreg_pi"), True)


if __name__ == "__main__":
    unittest.main()
