"""Source plug-in interface for the zero-server scan (standard library only).

Every source kind is one module, ``zero/sources/<kind>.py``, holding one
class registered under its kind name. A kind implements two steps:

    fetch(window) -> raw records   (a ``Batch``: a list plus ``truncated``
                                    and ``errors``)
    normalize(raw) -> items        (dicts in the item shape)

``Source.run(window)`` chains them and never drops anything silently: a
failed page, a refused URL or a record that cannot be normalized becomes an
entry in ``RunResult.errors``.

Network access goes through ``PoliteFetcher``:
  - a clear User-Agent (product, version and a contact role, never a
    personal name; set ``ZERO_USER_AGENT`` or ``ZERO_CONTACT``);
  - only the hosts the source declares (``hosts:`` in its YAML file);
  - robots.txt honoured per host, fetched once and cached; a disallowed
    path is a recorded source failure;
  - one request at a time per host, spaced by the host delay
    (``limits.host_delay_ms``; floor 250 ms, ceiling 10000 ms, default 1000).

For the simulator and the tests, ``FixtureTransport`` replays the recorded
or synthesized responses under ``zero/fixtures/`` instead of the network.

Also here: a small YAML-subset reader (block mappings and lists, flow lists,
quoted and plain scalars, comments), enough for ``zero/pack/`` and
``zero/policies.yaml`` without a third-party package.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser

PRODUCT = "RegWatch-zero"
VERSION = "0.1"
DEFAULT_CONTACT = "contact: the deploying organization's government-affairs role address"

HOST_DELAY_FLOOR_MS = 250
HOST_DELAY_CEILING_MS = 10000
HOST_DELAY_DEFAULT_MS = 1000
DEFAULT_TIMEOUT_S = 30

ZERO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES_ROOT = ZERO_ROOT / "fixtures"
PACK_ROOT = ZERO_ROOT / "pack"


# ---------------------------------------------------------------------------
# Errors and results


class SourceError(Exception):
    """A recorded source failure (HTTP error, refused URL, malformed page)."""


class RobotsDisallowed(SourceError):
    """robots.txt disallows the URL for our User-Agent."""


class HostNotAllowed(SourceError):
    """The URL's host is not declared by the source."""


class NormalizeError(ValueError):
    """A raw record could not be turned into an item."""


class Batch(list):
    """The raw records of one fetch, plus whether it was cut short and why.

    ``truncated`` is true when a page cap or item cap stopped the fetch with
    more data left. ``errors`` lists dicts with ``source``, ``stage``
    (``page``, ``body`` or ``normalize``), ``url`` and ``error``.
    """

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.truncated = False
        self.errors = []


@dataclass
class RunResult:
    source: str
    kind: str
    items: list = field(default_factory=list)
    raw_count: int = 0
    truncated: bool = False
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Window:
    """An inclusive range of calendar dates to scan."""

    start: _dt.date
    end: _dt.date

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before start {self.start}")

    @classmethod
    def lookback(cls, days: int, today: _dt.date | None = None) -> "Window":
        today = today or _dt.date.today()
        if days < 1:
            raise ValueError("lookback days must be at least 1")
        return cls(today - _dt.timedelta(days=days - 1), today)

    @classmethod
    def parse(cls, start: str, end: str | None = None) -> "Window":
        s = _dt.date.fromisoformat(start)
        return cls(s, _dt.date.fromisoformat(end) if end else s)

    def days(self):
        d = self.start
        while d <= self.end:
            yield d
            d += _dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Registry


REGISTRY: dict = {}


def register(kind: str):
    """Class decorator: register a Source subclass under its kind name."""

    def deco(cls):
        if kind in REGISTRY and REGISTRY[kind] is not cls:
            raise ValueError(f"source kind {kind!r} registered twice")
        cls.kind = kind
        REGISTRY[kind] = cls
        return cls

    return deco


def get_kind(kind: str):
    """Return the class for a kind, importing ``<package>.<kind>`` on demand."""
    if kind not in REGISTRY:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kind or ""):
            raise KeyError(f"invalid source kind {kind!r}")
        pkg = __package__ or "sources"
        try:
            importlib.import_module(f"{pkg}.{kind}")
        except ModuleNotFoundError as e:
            raise KeyError(f"unknown source kind {kind!r}") from e
    if kind not in REGISTRY:
        raise KeyError(f"module for kind {kind!r} registered nothing")
    return REGISTRY[kind]


def create(source: dict, fetcher: "PoliteFetcher | None" = None, **options) -> "Source":
    """Build the connector for a source definition (a parsed source YAML)."""
    return get_kind(source.get("kind"))(source, fetcher=fetcher, **options)


# ---------------------------------------------------------------------------
# HTTP


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SourceError(f"malformed JSON from {self.url}: {e}") from e


def urllib_transport(url: str, headers: dict, timeout: float) -> Response:
    """The live transport: one GET with urllib. HTTP errors become Responses."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (hosts are checked by the caller)
            return Response(url, r.status, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Response(url, e.code, e.read() or b"", dict(e.headers or {}))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError(f"request failed for {url}: {e}") from e


def default_user_agent() -> str:
    ua = os.environ.get("ZERO_USER_AGENT", "").strip()
    if ua:
        return ua
    contact = os.environ.get("ZERO_CONTACT", "").strip() or DEFAULT_CONTACT
    if not contact.startswith("contact"):
        contact = f"contact: {contact}"
    return f"{PRODUCT}/{VERSION} (public-records monitor; {contact})"


def clamp_delay_ms(ms) -> int:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = HOST_DELAY_DEFAULT_MS
    return max(HOST_DELAY_FLOOR_MS, min(HOST_DELAY_CEILING_MS, ms))


class PoliteFetcher:
    """GETs one URL at a time per host, within the declared hosts, obeying robots.txt.

    ``transport(url, headers, timeout) -> Response`` is injectable (the
    fixture transport, or a fake in tests); ``clock`` and ``sleep`` too.
    One fetcher can be shared by every source in a run, so the delay for a
    host is the largest any source declared for it.
    """

    def __init__(self, user_agent: str | None = None, transport=None,
                 host_delay_ms: int = HOST_DELAY_DEFAULT_MS, honor_robots: bool = True,
                 timeout_s: float = DEFAULT_TIMEOUT_S, clock=time.monotonic, sleep=time.sleep):
        self.user_agent = user_agent or default_user_agent()
        self.transport = transport or urllib_transport
        self.default_delay_ms = clamp_delay_ms(host_delay_ms)
        self.honor_robots = honor_robots
        self.timeout_s = timeout_s
        self.clock = clock
        self.sleep = sleep
        self._delay_ms: dict = {}
        self._last: dict = {}
        self._robots: dict = {}
        self.requests: list = []  # (host, url, clock time) for the run record and tests

    def declare_host(self, host: str, delay_ms=None):
        host = host.lower()
        d = clamp_delay_ms(delay_ms if delay_ms is not None else self.default_delay_ms)
        self._delay_ms[host] = max(d, self._delay_ms.get(host, 0))

    def delay_for(self, host: str) -> int:
        return self._delay_ms.get(host.lower(), self.default_delay_ms)

    def _wait_turn(self, host: str):
        last = self._last.get(host)
        if last is not None:
            due = last + self.delay_for(host) / 1000.0
            now = self.clock()
            if now < due:
                self.sleep(due - now)
        self._last[host] = self.clock()

    def _raw_get(self, url: str, host: str, timeout: float) -> Response:
        self._wait_turn(host)
        self.requests.append((host, url, self._last[host]))
        headers = {"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
        return self.transport(url, headers, timeout)

    def _robots_for(self, scheme: str, host: str, timeout: float):
        if host in self._robots:
            return self._robots[host]
        url = f"{scheme}://{host}/robots.txt"
        rp = urllib.robotparser.RobotFileParser(url)
        try:
            resp = self._raw_get(url, host, timeout)
        except SourceError as e:
            # Cannot read robots.txt: be conservative, refuse this host for the run.
            rp = None
            self._robots[host] = (rp, f"robots.txt unreadable: {e}")
            return self._robots[host]
        if resp.status in (401, 403):
            rp.disallow_all = True
        elif 400 <= resp.status < 500:
            rp.allow_all = True
        elif resp.status >= 500:
            self._robots[host] = (None, f"robots.txt returned HTTP {resp.status}")
            return self._robots[host]
        else:
            rp.parse(resp.text().splitlines())
        self._robots[host] = (rp, None)
        return self._robots[host]

    def get(self, url: str, hosts=None, timeout_s: float | None = None) -> Response:
        """GET a URL; raise SourceError for a refused URL or a non-2xx answer."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise SourceError(f"refused non-HTTP URL {url!r}")
        host = parts.hostname.lower()
        if hosts is not None and host not in {h.lower() for h in hosts}:
            raise HostNotAllowed(f"host {host} is not declared by the source")
        timeout = timeout_s or self.timeout_s
        if self.honor_robots:
            rp, problem = self._robots_for(parts.scheme, host, timeout)
            if rp is None:
                raise SourceError(f"{problem}; refusing {url}")
            if not rp.can_fetch(self.user_agent, url):
                raise RobotsDisallowed(f"robots.txt disallows {url}")
        resp = self._raw_get(url, host, timeout)
        if not 200 <= resp.status < 300:
            raise SourceError(f"HTTP {resp.status} for {url}")
        return resp

    def get_json(self, url: str, hosts=None, timeout_s: float | None = None):
        return self.get(url, hosts=hosts, timeout_s=timeout_s).json()


# ---------------------------------------------------------------------------
# Fixtures (simulator mode and tests)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fixture_key(url: str):
    """Match key for a request: host and path, the ISO dates in the query, the page.

    The connector's live query (field list, per_page, order, agency
    conditions) need not equal the one stored in the fixture; a request
    matches the fixture for the same endpoint, dates and page.
    """
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    dates = tuple(sorted({v for _, v in q if _ISO_DATE.match(v)}))
    page = 1
    for k, v in q:
        if k == "page":
            try:
                page = int(v)
            except ValueError:
                pass
    return ((parts.hostname or "").lower() + parts.path, dates, page)


def iter_fixture_files(root=FIXTURES_ROOT):
    root = pathlib.Path(root)
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            yield p


class FixtureTransport:
    """A transport that answers from fixture files instead of the network.

    A fixture is a JSON file with ``request.url``, ``status`` and
    ``response`` (the decoded body). Unknown URLs, robots.txt included,
    answer 404.
    """

    def __init__(self, *roots):
        self.index: dict = {}
        for root in roots or (FIXTURES_ROOT,):
            for p in iter_fixture_files(root):
                if p.suffix != ".json":
                    continue
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(doc, dict) or not isinstance(doc.get("request"), dict):
                    continue
                url = doc["request"].get("url")
                if not url:
                    continue
                key = fixture_key(url)
                if key in self.index:
                    raise ValueError(f"fixtures {self.index[key][0]} and {p} answer the same request")
                self.index[key] = (p, doc)
        self.served: list = []

    def __call__(self, url, headers, timeout) -> Response:
        hit = self.index.get(fixture_key(url))
        if hit is None:
            return Response(url, 404, b"no fixture for this request")
        p, doc = hit
        self.served.append(p)
        body = doc.get("response")
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        return Response(url, int(doc.get("status", 200)), data, {"X-Fixture": str(p)})


def fixture_fetcher(*roots, **kw) -> PoliteFetcher:
    """A PoliteFetcher over FixtureTransport that does not actually sleep."""
    kw.setdefault("sleep", lambda s: None)
    return PoliteFetcher(transport=FixtureTransport(*roots), **kw)


# ---------------------------------------------------------------------------
# The Source base class


class Source:
    """Base for every kind. Subclasses set ``kind`` via ``@register`` and
    implement ``fetch`` and ``normalize_one``."""

    kind: str = ""

    def __init__(self, source: dict, fetcher: PoliteFetcher | None = None, **options):
        if not isinstance(source, dict) or not source.get("id"):
            raise ValueError("a source definition needs an id")
        if source.get("data_class") != "public":
            # Hard limit: public sources only.
            raise ValueError(f"source {source.get('id')!r} is not data_class: public; refused")
        self.source = source
        self.id = source["id"]
        self.config = source.get("config") or {}
        self.limits = source.get("limits") or {}
        self.hosts = [h.lower() for h in (source.get("hosts") or [])]
        if not self.hosts:
            raise ValueError(f"source {self.id!r} declares no hosts")
        self.options = options
        self.fetcher = fetcher or PoliteFetcher(timeout_s=self.timeout_s)
        for h in self.hosts:
            self.fetcher.declare_host(h, self.limits.get("host_delay_ms"))

    # limits
    @property
    def page_cap(self) -> int:
        return int(self.options.get("page_cap") or self.limits.get("page_cap") or 10)

    @property
    def max_items(self) -> int:
        return int(self.options.get("max_items") or self.limits.get("max_items") or 200)

    @property
    def timeout_s(self) -> float:
        return float(self.limits.get("timeout_s") or DEFAULT_TIMEOUT_S)

    def get_json(self, url: str):
        return self.fetcher.get_json(url, hosts=self.hosts, timeout_s=self.timeout_s)

    def get(self, url: str) -> Response:
        return self.fetcher.get(url, hosts=self.hosts, timeout_s=self.timeout_s)

    def error(self, stage: str, url, err) -> dict:
        return {"source": self.id, "kind": self.kind, "stage": stage, "url": url, "error": str(err)}

    # the interface
    def fetch(self, window: Window) -> Batch:
        raise NotImplementedError

    def normalize_one(self, raw: dict) -> dict:
        raise NotImplementedError

    def normalize(self, raw) -> list:
        """Raw records to items; raises NormalizeError on the first bad record."""
        return [self.normalize_one(r) for r in raw]

    def run(self, window: Window) -> RunResult:
        """fetch then normalize, recording every failure instead of dropping it."""
        res = RunResult(self.id, self.kind)
        try:
            batch = self.fetch(window)
        except SourceError as e:
            res.errors.append(self.error("fetch", None, e))
            return res
        res.raw_count = len(batch)
        res.truncated = batch.truncated
        res.errors.extend(batch.errors)
        for r in batch:
            try:
                res.items.append(self.normalize_one(r))
            except (NormalizeError, KeyError, TypeError, ValueError) as e:
                ident = r.get("document_number") if isinstance(r, dict) else None
                res.errors.append(self.error("normalize", ident, e))
        return res


# ---------------------------------------------------------------------------
# Helpers shared by kinds

_TEMPLATE = re.compile(r"\{(\w+)\}")


def fill_template(template: str, record: dict) -> str:
    def sub(m):
        v = record.get(m.group(1))
        if v is None or v == "":
            raise NormalizeError(f"template field {m.group(1)!r} missing from record")
        return str(v)

    return _TEMPLATE.sub(sub, template)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def agency_slugs(source: dict, watchlist: dict | None = None) -> list:
    """Federal Register agency slugs for a source's ``agencies`` ids, via watchlist.yaml."""
    if watchlist is None:
        wl_path = PACK_ROOT / "watchlist.yaml"
        watchlist = load_yaml(wl_path) if wl_path.is_file() else {}
    by_id = {a.get("id"): a for a in (watchlist.get("agencies") or []) if isinstance(a, dict)}
    slugs = []
    for aid in source.get("agencies") or []:
        a = by_id.get(aid)
        if not a or not a.get("fedreg_slug"):
            raise ValueError(f"source {source.get('id')!r}: agency {aid!r} has no fedreg_slug in the watchlist")
        slugs.append(a["fedreg_slug"])
    return slugs


# ---------------------------------------------------------------------------
# A YAML subset (enough for zero/pack and zero/policies.yaml)


class YamlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_flow(s: str) -> list:
    items, cur, quote, depth = [], [], None, 0
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail or items:
        items.append(tail)
    return [i for i in items if i != ""]


def _scalar(s: str):
    s = s.strip()
    if s == "":
        return None
    if s.startswith('"'):
        if not s.endswith('"') or len(s) < 2:
            raise YamlError(f"unterminated string: {s}")
        return json.loads(s)
    if s.startswith("'"):
        if not s.endswith("'") or len(s) < 2:
            raise YamlError(f"unterminated string: {s}")
        return s[1:-1].replace("''", "'")
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlError(f"unterminated flow list: {s}")
        return [_scalar(x) for x in _split_flow(s[1:-1])]
    if s.startswith("{"):
        if not s.endswith("}"):
            raise YamlError(f"unterminated flow mapping: {s}")
        out = {}
        for part in _split_flow(s[1:-1]):
            k, sep, v = part.partition(":")
            if not sep:
                raise YamlError(f"bad flow mapping entry: {part}")
            out[_scalar(k)] = _scalar(v)
        return out
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
        return float(s)
    return s


def _split_key(content: str):
    """Split ``key: value`` at the first colon outside quotes; None if no key."""
    quote = None
    for i, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and i == 0:
            quote = ch
        elif ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
            return _scalar(content[:i]) if content[0] in "\"'" else content[:i].strip(), content[i + 1:].strip()
    return None


def load_yaml_text(text: str):
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {n}: tab indentation")
        s = _strip_comment(raw)
        if s.strip() in ("", "---"):
            continue
        if s.strip() == "...":
            break
        lines.append((len(s) - len(s.lstrip(" ")), s.strip(), n))
    if not lines:
        return None
    pos = [0]

    def parse_block(indent):
        ind, content, n = lines[pos[0]]
        if content == "-" or content.startswith("- "):
            return parse_list(ind)
        return parse_map(ind)

    def parse_value(rest, parent_indent, n):
        if rest != "":
            return _scalar(rest)
        if pos[0] < len(lines):
            ind, content, _ = lines[pos[0]]
            if ind > parent_indent or (ind == parent_indent and (content == "-" or content.startswith("- "))):
                return parse_block(ind)
        return None

    def parse_map(indent, first=None):
        out = {}
        while pos[0] < len(lines):
            if first is not None:
                content, n = first
                first = None
            else:
                ind, content, n = lines[pos[0]]
                if ind < indent:
                    break
                if ind > indent:
                    raise YamlError(f"line {n}: unexpected indentation")
                if content == "-" or content.startswith("- "):
                    break
                pos[0] += 1
            kv = _split_key(content)
            if kv is None:
                raise YamlError(f"line {n}: expected 'key: value'")
            k, rest = kv
            if k in out:
                raise YamlError(f"line {n}: duplicate key {k!r}")
            out[k] = parse_value(rest, indent, n)
        return out

    def parse_list(indent):
        out = []
        while pos[0] < len(lines):
            ind, content, n = lines[pos[0]]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                if ind > indent:
                    raise YamlError(f"line {n}: unexpected indentation")
                break
            pos[0] += 1
            item = content[1:].strip()
            if item == "":
                out.append(parse_value("", indent, n))
            elif not item.startswith(("[", "{")) and _split_key(item) is not None:
                out.append(parse_map(indent + 2, first=(item, n)))
            else:
                out.append(_scalar(item))
        return out

    result = parse_block(lines[0][0])
    if pos[0] < len(lines):
        raise YamlError(f"line {lines[pos[0]][2]}: unexpected content")
    return result


def load_yaml(path):
    return load_yaml_text(pathlib.Path(path).read_text(encoding="utf-8"))


def load_source_file(path) -> dict:
    src = load_yaml(path)
    if not isinstance(src, dict):
        raise YamlError(f"{path}: a source file must be a mapping")
    return src
