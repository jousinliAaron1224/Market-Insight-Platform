"""財經新聞 RSS（D 級，決策 D9／D10／D16）。

來源（config/sources.yaml 的 feeds）：
- 工商時報「保險脈動」RSS：保險專版，全收；約半數為公關稿，以規則標記 pr_noise（D10）。
- 中央社財經 RSS：內容很雜，標題或導言含關鍵字（保險、壽險…）才收（D9）。

著作權（Handbook／D16）：
- raw 只存標題、連結、發布時間、guid 等中繼資料（永久）。
- RSS 導言只放 news_leads 暫存表供分類／摘要，lead_ttl_days 後清除；不下載新聞內文頁。
- fetch 不發額外請求。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from adapters.base import ItemRef, RawDoc, SourceAdapter
from adapters.common import TPE, html_to_text
from core.storage import utcnow

DEFAULT_KEYWORDS = ["保險", "壽險", "產險", "保單", "保費", "金管會"]
DEFAULT_PR_PATTERNS = [r"勇奪", r"榮獲", r"奪.{0,12}獎", r"獲.{0,20}獎", r"獎項", r"大獎", r"評比",
                       r"公益", r"捐贈", r"捐款", r"志工", r"路跑", r"公開賽", r"盃", r"音樂會", r"頒獎"]


def parse_pubdate(s: str) -> datetime | None:
    """RFC 822（中央社）或無時區的 ISO（工商時報，視為台北時間）。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=TPE)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


class NewsRssAdapter(SourceAdapter):
    source_id = "news_rss"
    tier = "D"
    domain = "multiple"  # 各 feed 依自己的網域限速

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.feeds: list[dict[str, Any]] = config["feeds"]
        self.keywords: list[str] = config.get("keywords", DEFAULT_KEYWORDS)
        self.pr_patterns = [re.compile(p) for p in config.get("pr_noise_patterns", DEFAULT_PR_PATTERNS)]
        self.lead_ttl_days = int(config.get("lead_ttl_days", 30))
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._entries: dict[str, dict[str, Any]] = {}
        self.list_errors: list[str] = []  # 部分 feed 失敗時由 change_detect 記入 crawl_runs

    # ---- 列表層 ----
    def list_items(self) -> list[ItemRef]:
        self._entries.clear()
        refs: list[ItemRef] = []
        errors: list[str] = []
        active = [f for f in self.feeds if f.get("enabled", True)]
        for feed in active:
            try:
                resp = self.ctx.http.get(feed["url"], respect_robots=self.respect_robots)
                refs.extend(self.parse_feed(feed, resp.content))
            except Exception as e:  # 單一 feed 失敗不影響其他 feed
                errors.append(f"{feed['name']}: {e!r}")
        # 全部 feed 都失敗才算整輪失敗；某 feed 成功但過濾後 0 筆是正常情況
        if active and len(errors) == len(active):
            raise RuntimeError("; ".join(errors))
        self.list_errors = errors
        return refs

    def parse_feed(self, feed: dict[str, Any], data: bytes) -> list[ItemRef]:
        parsed = feedparser.parse(data)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"{feed['name']} parse error: {parsed.bozo_exception!r}")
        refs = []
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            lead = html_to_text(e.get("description") or "")
            matched = [k for k in self.keywords if k in title or k in lead]
            if feed.get("keyword_filter") and not matched:
                continue
            guid = (e.get("id") or e.get("link") or "").strip()
            if not guid:
                continue
            key = f"{feed['name']}:{guid}"
            pub = parse_pubdate(e.get("published", ""))
            pr_hits = [p.pattern for p in self.pr_patterns if p.search(title)]
            self._entries[key] = {
                "feed": feed["name"], "guid": guid, "title": title,
                "link": (e.get("link") or "").strip(), "pubDate": e.get("published", ""),
                "lead": lead, "matched_keywords": matched, "pr_hits": pr_hits,
            }
            refs.append(ItemRef(self.source_id, key, self._entries[key]["link"], title or None, pub))
        return refs

    # ---- 單一項目：只存中繼資料；導言進暫存表 ----
    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        e = self._entries.get(ref.item_key)
        if e is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        record = {k: e[k] for k in ("feed", "guid", "title", "link", "pubDate")}  # 不含導言
        data = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        path, digest = self.ctx.raw.put(self.source_id, data, "json", now)
        pr_noise = bool(e["pr_hits"])
        meta: dict[str, Any] = {
            "title": ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "feed": e["feed"],
            "matched_keywords": e["matched_keywords"],
            "pr_noise": pr_noise,
            "pr_hits": e["pr_hits"],
            "event_extra": {"feed": e["feed"], "pr_noise": pr_noise},
            "transient_lead": e["lead"] or None,
            "lead_ttl_days": self.lead_ttl_days,
        }
        return RawDoc(self.source_id, ref.item_key, ref.url, now, digest, "rss_item",
                      path, None, None, meta)
