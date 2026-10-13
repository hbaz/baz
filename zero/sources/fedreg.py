"""Kind ``fedreg``: Federal Register documents (published), standard library only.

One query per day of the window (``date_from_param`` and ``date_to_param``
both set to that day), with every watched agency slug as a repeated
``agency_param`` and every ``document_types`` value as a repeated
``type_param``. Each query follows ``next_page_field`` until it is absent
or ``limits.page_cap`` pages were read.

Paging: this module pages with its own loop (instance 1 of the paging
pattern; instance 2 is fedreg_pi.py). T237 moves it onto
``base.walk_pages``. Semantics, which the shared primitive must keep:
  - a page cap reached with a next link left sets ``truncated``;
  - ``limits.max_items`` reached with records left sets ``truncated``;
  - a failed page is recorded in ``errors`` and stops that query (the
    other days still run); nothing is dropped silently;
  - a document seen twice (on two days or pages) is kept once.

VERIFY before the first live run (see zero/pack/sources/fedreg.yaml): the
parameter and field names against
https://www.federalregister.gov/developers/documentation/api/v1
"""

from __future__ import annotations

import urllib.parse

from . import base

TYPE_STAGE = {
    "proposed rule": "proposed",
    "rule": "final",
    "notice": "notice",
    "presidential document": "other",
}


def stage_for_type(doc_type) -> str:
    return TYPE_STAGE.get(str(doc_type or "").strip().lower(), "other")


def agency_names(record: dict) -> list:
    out = []
    for a in record.get("agencies") or []:
        if isinstance(a, dict):
            name = a.get("name") or a.get("raw_name")
            if name:
                out.append(name)
        elif isinstance(a, str):
            out.append(a)
    return out


def agency_slug_list(record: dict) -> list:
    return [a["slug"] for a in record.get("agencies") or [] if isinstance(a, dict) and a.get("slug")]


def text_block(source: base.Source, record: dict) -> dict:
    text = {}
    for f in source.config.get("text_fields") or []:
        v = record.get("_body") if f == "body" else record.get(f)
        if isinstance(v, str) and v.strip():
            text[f] = v
    return text


def date_block(source: base.Source, record: dict) -> dict:
    return {k: record.get(f) for k, f in (source.config.get("date_fields") or {}).items()}


@base.register("fedreg")
class FedRegSource(base.Source):
    """Options: ``agency_slugs`` (list, default from the watchlist),
    ``fetch_body`` (bool, default from config), ``page_cap``, ``max_items``."""

    def __init__(self, source, fetcher=None, **options):
        super().__init__(source, fetcher=fetcher, **options)
        slugs = options.get("agency_slugs")
        self.agency_slugs = list(slugs) if slugs is not None else base.agency_slugs(source)

    @property
    def fetch_body(self) -> bool:
        v = self.options.get("fetch_body")
        return bool(self.config.get("fetch_body")) if v is None else bool(v)

    def first_page_url(self, day) -> str:
        c = self.config
        q = []
        for f in c.get("fields") or []:
            q.append(("fields[]", f))
        if c.get("per_page"):
            q.append(("per_page", str(c["per_page"])))
        if c.get("order"):
            q.append(("order", c["order"]))
        for t in c.get("document_types") or []:
            q.append((c.get("type_param", "conditions[type][]"), t))
        for s in self.agency_slugs:
            q.append((c.get("agency_param", "conditions[agencies][]"), s))
        iso = day.isoformat()
        q.append((c.get("date_from_param", "conditions[publication_date][gte]"), iso))
        q.append((c.get("date_to_param", "conditions[publication_date][lte]"), iso))
        return self.source["url"] + "?" + urllib.parse.urlencode(q)

    def fetch(self, window: base.Window) -> base.Batch:
        batch = base.Batch()
        seen = set()
        results_field = self.config.get("results_field", "results")
        next_field = self.config.get("next_page_field", "next_page_url")
        full = False
        for day in window.days():
            if full:
                break
            url = self.first_page_url(day)
            pages = 0
            while url:
                if pages >= self.page_cap:
                    batch.truncated = True
                    break
                try:
                    data = self.get_json(url)
                except base.SourceError as e:
                    batch.errors.append(self.error("page", url, e))
                    break
                pages += 1
                results = data.get(results_field) if isinstance(data, dict) else None
                if not isinstance(results, list):
                    batch.errors.append(self.error("page", url, f"no {results_field!r} list in the response"))
                    break
                for rec in results:
                    key = rec.get("document_number") if isinstance(rec, dict) else None
                    if key is not None and key in seen:
                        continue
                    if len(batch) >= self.max_items:
                        batch.truncated = True
                        full = True
                        break
                    if key is not None:
                        seen.add(key)
                    batch.append(rec)
                if full:
                    break
                url = data.get(next_field) or None
        if self.fetch_body:
            self._attach_bodies(batch)
        return batch

    def _attach_bodies(self, batch: base.Batch):
        """Full text for the verbatim citation check; a failure is recorded, the record kept."""
        for rec in batch:
            if not isinstance(rec, dict):
                continue
            url = rec.get("body_html_url")
            if not url:
                continue
            try:
                rec["_body"] = base.html_to_text(self.get(url).text())
            except base.SourceError as e:
                batch.errors.append(self.error("body", url, e))

    def normalize_one(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise base.NormalizeError("record is not an object")
        dn = raw.get("document_number")
        if not dn:
            raise base.NormalizeError("record has no document_number")
        if not raw.get("title"):
            raise base.NormalizeError(f"{dn}: record has no title")
        c = self.config
        return {
            "id": base.fill_template(c.get("item_id", "fedreg-{document_number}"), raw),
            "source": self.id,
            "kind": self.kind,
            "doc_id": dn,
            "canonical_key": base.fill_template(c.get("canonical_key", "{document_number}"), raw),
            "title": raw.get("title"),
            "url": raw.get("html_url"),
            "type": raw.get("type"),
            "stage": stage_for_type(raw.get("type")),
            "action": raw.get("action"),
            "published": raw.get("publication_date"),
            "effective_on": raw.get("effective_on"),
            "comments_close_on": raw.get("comments_close_on"),
            "docket_ids": list(raw.get("docket_ids") or []),
            "regulation_id_numbers": list(raw.get("regulation_id_numbers") or []),
            "agencies": agency_names(raw),
            "agency_slugs": agency_slug_list(raw),
            "dates": date_block(self, raw),
            "text": text_block(self, raw),
            "pdf_url": raw.get("pdf_url"),
        }
