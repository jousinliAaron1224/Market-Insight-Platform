"""金管會裁罰案件 RSS（A 級，決策 D14，2026-10-02 新增）。

- RSS：https://www.fsc.gov.tw/RSS/Messages?serno=201202290003&language=chinese
  最近 10 筆，description 內含完整裁處書；無 ETag / Last-Modified。
- 全部收錄（量很少），以發文字號判斷是否為保險局案件：「金管保…」→ is_insurance=True。
- fetch 不發額外請求：RSS item（含裁處書全文）即為 raw 內容。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import feedparser

from adapters.base import ItemRef, RawDoc, SourceAdapter
from adapters.common import TPE, html_to_text
from core.storage import utcnow

BASE = "https://www.fsc.gov.tw/ch/"


def canonical_penalty_url(dataserno: str) -> str:
    return (f"{BASE}home.jsp?id=131&parentpath=0,2&mcustomize=multimessage_view.jsp"
            f"&dataserno={dataserno}&dtable=Penalty")


def _dataserno(link: str) -> str:
    q = parse_qs(urlsplit(link.replace("&amp;", "&")).query)
    return (q.get("dataserno") or [""])[0]


_DOC_NO = re.compile(r"發文字號[:：]\s*([^\s<]+)")
_RESPONDENT = re.compile(r"受處分人[:：]\s*([^\s<]+)")
_FINE = re.compile(r"新臺幣\s*(?:[（(][^）)]{0,6}[）)])?\s*([\d,]+(?:\.\d+)?)\s*(萬|億)?元")


def parse_penalty_fields(title: str, description_html: str) -> dict[str, Any]:
    text = html_to_text(description_html)
    meta: dict[str, Any] = {}
    if m := _DOC_NO.search(text):
        meta["doc_no"] = m.group(1)
    if m := _RESPONDENT.search(text):
        meta["respondent"] = m.group(1)
    if m := _FINE.search(title) or _FINE.search(text):
        amount = float(m.group(1).replace(",", ""))
        unit = {"萬": 1e4, "億": 1e8}.get(m.group(2) or "", 1)
        meta["fine_twd"] = int(amount * unit)
    doc_no = meta.get("doc_no", "")
    meta["is_insurance"] = doc_no.startswith("金管保") or "保險" in meta.get("respondent", "")
    missing = [k for k in ("doc_no", "respondent") if k not in meta]
    if missing:
        meta["parse_warnings"] = [f"missing {k}" for k in missing]
    return meta


class FscPenaltyAdapter(SourceAdapter):
    source_id = "fsc_penalty"
    tier = "A"
    domain = "www.fsc.gov.tw"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.feed_url: str = config["feed_url"]
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._entries: dict[str, dict[str, str]] = {}

    def list_items(self) -> list[ItemRef]:
        resp = self.ctx.http.get(self.feed_url, respect_robots=self.respect_robots)
        return self.parse_feed(resp.content)

    def parse_feed(self, data: bytes) -> list[ItemRef]:
        feed = feedparser.parse(data, sanitize_html=False)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feed parse error: {feed.bozo_exception!r}")
        self._entries.clear()
        refs = []
        for e in feed.entries:
            serno = _dataserno(e.get("link") or e.get("id") or "")
            if not serno:
                continue
            key = canonical_penalty_url(serno)
            pub = None
            if e.get("published_parsed"):
                y, mo, d = e.published_parsed[:3]  # 來源以 00:00 GMT 表示「當天」
                pub = datetime(y, mo, d, tzinfo=TPE)
            entry = {
                "dataserno": serno,
                "title": (e.get("title") or "").strip(),
                "pubDate": e.get("published", ""),
                "link": key,
                "description": e.get("description", ""),
            }
            self._entries[key] = entry
            refs.append(ItemRef(self.source_id, key, key, entry["title"] or None, pub))
        return refs

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        entry = self._entries.get(ref.item_key)
        if entry is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        data = json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        path, digest = self.ctx.raw.put(self.source_id, data, "json", now)
        fields = parse_penalty_fields(entry["title"], entry["description"])
        meta: dict[str, Any] = {
            "title": ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "dataserno": entry["dataserno"],
            **fields,
            "event_extra": {k: fields.get(k) for k in ("respondent", "is_insurance", "fine_twd")},
        }
        return RawDoc(self.source_id, ref.item_key, ref.url, now, digest, "rss_item",
                      path, None, None, meta)
