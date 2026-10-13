"""Kind ``fedreg_pi``: Federal Register Public Inspection (filed, not yet published).

One query per day of the window (``date_param`` set to that day, every
watched agency slug as a repeated ``agency_param``), following
``next_page_field`` until it is absent or ``limits.page_cap`` pages were
read. A PI item shares its ``item_id`` and ``canonical_key`` with the later
fedreg record, which replaces its stage when the document publishes.

Paging: this module pages with its own loop (instance 2 of the paging
pattern; instance 1 is fedreg.py). T237 moves it onto ``base.walk_pages``
with the same semantics: cap reached with a next link left or
``max_items`` reached with records left sets ``truncated``; a failed page
is recorded and stops that query; duplicates are kept once.

VERIFY before the first live run (see zero/pack/sources/fedreg_pi.yaml):
the endpoint, its date condition and whether it pages at all.
"""

from __future__ import annotations

import urllib.parse

from . import base
from .fedreg import agency_names, agency_slug_list, date_block, text_block


@base.register("fedreg_pi")
class FedRegPISource(base.Source):
    """Options: ``agency_slugs`` (list, default from the watchlist), ``page_cap``, ``max_items``."""

    def __init__(self, source, fetcher=None, **options):
        super().__init__(source, fetcher=fetcher, **options)
        slugs = options.get("agency_slugs")
        self.agency_slugs = list(slugs) if slugs is not None else base.agency_slugs(source)

    def first_page_url(self, day) -> str:
        c = self.config
        q = []
        if c.get("per_page"):
            q.append(("per_page", str(c["per_page"])))
        for s in self.agency_slugs:
            q.append((c.get("agency_param", "conditions[agencies][]"), s))
        q.append((c.get("date_param", "conditions[available_on]"), day.isoformat()))
        return self.source["url"] + "?" + urllib.parse.urlencode(q)

    def fetch(self, window: base.Window) -> base.Batch:
        batch = base.Batch()
        seen = set()
        results_field = self.config.get("results_field", "results")
        next_field = self.config.get("next_page_field", "next_page_url")
        days = list(window.days())
        d = 0
        while d < len(days):
            url = self.first_page_url(days[d])
            d += 1
            pages = 0
            stop_all = False
            while url is not None:
                if pages == self.page_cap:
                    batch.truncated = True
                    break
                try:
                    data = self.get_json(url)
                except base.SourceError as e:
                    batch.errors.append(self.error("page", url, e))
                    break
                pages += 1
                if not isinstance(data, dict) or not isinstance(data.get(results_field), list):
                    batch.errors.append(self.error("page", url, f"no {results_field!r} list in the response"))
                    break
                for rec in data[results_field]:
                    key = rec.get("document_number") if isinstance(rec, dict) else None
                    if key is not None and key in seen:
                        continue
                    if len(batch) == self.max_items:
                        batch.truncated = True
                        stop_all = True
                        break
                    if key is not None:
                        seen.add(key)
                    batch.append(rec)
                if stop_all:
                    break
                url = data.get(next_field) or None
            if stop_all:
                break
        return batch

    def normalize_one(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise base.NormalizeError("record is not an object")
        dn = raw.get("document_number")
        if not dn:
            raise base.NormalizeError("record has no document_number")
        title = raw.get("title") or " ".join(
            s for s in (raw.get("toc_subject"), raw.get("toc_doc")) if s
        )
        if not title:
            raise base.NormalizeError(f"{dn}: record has no title")
        rec = dict(raw, title=title)
        c = self.config
        return {
            "id": base.fill_template(c.get("item_id", "fedreg-{document_number}"), rec),
            "source": self.id,
            "kind": self.kind,
            "doc_id": dn,
            "canonical_key": base.fill_template(c.get("canonical_key", "{document_number}"), rec),
            "title": title,
            "url": raw.get("html_url"),
            "type": raw.get("type"),
            "stage": c.get("stage", "pre_publication"),
            "filed_at": raw.get("filed_at"),
            "published": None,
            "scheduled_publication": raw.get("publication_date"),
            "docket_ids": list(raw.get("docket_numbers") or []),
            "agencies": agency_names(raw),
            "agency_slugs": agency_slug_list(raw),
            "dates": date_block(self, rec),
            "text": text_block(self, rec),
            "pdf_url": raw.get("pdf_url"),
        }
