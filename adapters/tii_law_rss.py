"""保發中心「保險法規最新動態」RSS（A 級）。

- 列表：https://law.tii.org.tw/Fn/rss.asp（RSS 2.0、UTF-8、最近 50 筆）
- 內文：ShowNews.asp?id=N（Big5，含資料類別、日期、條文、附件 PDF 連結）

來源特性（2026-10-02 取樣）：
- pubDate 為民國年 yyyMMdd（例 1150930），不是 RFC 822，需自行解析。
- link 為 http://，網站有 HSTS，正規化為 https。
- 無 ETag / Last-Modified（Cache-Control: no-store），HTTP 304 層對此來源不會觸發。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
from selectolax.parser import HTMLParser

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from core.storage import utcnow

TPE = ZoneInfo("Asia/Taipei")
_ROC_DATE = re.compile(r"^\s*(\d{2,3})[./-]?(\d{2})[./-]?(\d{2})\s*$")


def parse_roc_date(s: str | None) -> datetime | None:
    """'1150930' 或 '115.09.30' -> 2026-09-30 00:00 Asia/Taipei"""
    if not s:
        return None
    m = _ROC_DATE.match(s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return datetime(y + 1911, mo, d, tzinfo=TPE)
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    return re.sub(r"^http://", "https://", url.strip(), flags=re.I)


def parse_detail(html_bytes: bytes, base_url: str) -> dict[str, Any]:
    """從 ShowNews.asp 取出來源特有欄位；解析失敗不丟例外，改記 parse_warnings。"""
    text = html_bytes.decode("cp950", errors="replace")  # Big5 的 Windows 超集
    tree = HTMLParser(text)
    meta: dict[str, Any] = {}
    warnings: list[str] = []

    dt = tree.css_first("div.DataType")
    if dt:
        meta["data_type"] = dt.text(strip=True).split("：", 1)[-1]
    else:
        warnings.append("missing DataType")

    head = tree.css_first("table.news-table tr.bg01")
    if head:
        cells = head.css("td")
        if len(cells) >= 2:
            meta["announced_date"] = cells[0].text(strip=True)
            meta["detail_title"] = cells[1].text(strip=True)
    if "detail_title" not in meta:
        warnings.append("missing title row")

    pre = tree.css_first("table.news-table pre")
    meta["body_chars"] = len(pre.text()) if pre else 0
    if not pre:
        warnings.append("missing body <pre>")

    meta["attachments"] = [
        {"name": a.text(strip=True), "url": urljoin(base_url, a.attributes.get("href", ""))}
        for a in tree.css("table.news-table a")
        if "download.asp" in (a.attributes.get("href") or "")
    ]
    if warnings:
        meta["parse_warnings"] = warnings
    return meta


class TiiLawRssAdapter(SourceAdapter):
    source_id = "tii_law_rss"
    tier = "A"
    domain = "law.tii.org.tw"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.feed_url: str = config["feed_url"]
        self.fetch_detail: bool = bool(config.get("fetch_detail", True))
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._entries: dict[str, dict[str, str]] = {}

    # ---- 列表層：只抓 feed，不碰內文 ----
    def list_items(self) -> list[ItemRef]:
        resp = self.ctx.http.get(self.feed_url, respect_robots=self.respect_robots)
        return self.parse_feed(resp.content)

    def parse_feed(self, data: bytes) -> list[ItemRef]:
        feed = feedparser.parse(data)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feed parse error: {feed.bozo_exception!r}")
        refs: list[ItemRef] = []
        self._entries.clear()
        for e in feed.entries:
            raw_pub = e.get("published") or e.get("pubdate") or ""
            entry = {
                "guid": (e.get("id") or "").strip(),
                "title": (e.get("title") or "").strip(),
                "link": (e.get("link") or "").strip(),
                "pubDate": raw_pub.strip(),
            }
            key = entry["guid"] or normalize_url(entry["link"])
            if not key:
                continue
            self._entries[key] = entry
            refs.append(ItemRef(
                source_id=self.source_id,
                item_key=key,
                url=normalize_url(entry["link"]),
                title=entry["title"] or None,
                published_at=parse_roc_date(raw_pub),
            ))
        return refs

    # ---- 單一項目：RSS item 快照 + 內文頁，皆存 raw ----
    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        entry = self._entries.get(ref.item_key) or {
            "guid": ref.item_key, "title": ref.title or "", "link": ref.url,
            "pubDate": ref.published_at.strftime("%Y-%m-%d") if ref.published_at else "",
        }
        item_bytes = json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        item_path, item_hash = self.ctx.raw.put(self.source_id, item_bytes, "json", now)

        meta: dict[str, Any] = {
            "title": ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "rss_item_path": item_path,
            "rss_item_hash": item_hash,
        }
        if not self.fetch_detail:
            return RawDoc(self.source_id, ref.item_key, ref.url, now, item_hash, "rss_item",
                          item_path, None, None, meta)

        etag, last_mod = self.ctx.validators(self.source_id, ref.item_key)
        resp = self.ctx.http.get(ref.url, etag=etag, last_modified=last_mod,
                                 respect_robots=self.respect_robots)
        if resp.status_code == 304:
            raise NotModified(ref.url)
        html_path, html_hash = self.ctx.raw.put(self.source_id, resp.content, "html", now)
        meta.update(parse_detail(resp.content, ref.url))
        return RawDoc(
            source_id=self.source_id,
            item_key=ref.item_key,
            url=ref.url,
            fetched_at=now,
            content_hash=html_hash,
            doc_type="html",
            raw_path=html_path,
            http_etag=resp.headers.get("ETag"),
            http_last_modified=resp.headers.get("Last-Modified"),
            meta=meta,
        )
