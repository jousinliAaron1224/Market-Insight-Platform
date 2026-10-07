"""金管會法規草案預告 RSS（A 級，2026-10-05 新增）。

讓商品與法遵在法規「定案前」就看到草案（定案後的令由 tii_law_rss／fsc_press 涵蓋）。

- RSS：https://www.fsc.gov.tw/RSS/Noticelaw?serno=201202290010&language=chinese
  注意路徑是 /RSS/Noticelaw；同一個 serno 用 /RSS/Messages 會回傳 0 筆的空 channel。
  最近 20 筆、UTF-8；description 內含完整公告（發文日期、字號、主旨、承辦單位、陳述意見期限）。
  robots.txt 只擋 Googlebot 的 /uploaddowndoc，本來源不受限制。
- 全部收錄（量很少），以 category cake=490（保險）、字號「金管保」或承辦單位「保險局」判斷 is_insurance；
  非保險的草案由解析層預設隱藏，與 fsc_penalty 的作法相同。
- 陳述意見期限從公報刊登翌日起算，RSS 沒有刊登日，所以只記「N 日內」；
  標題有「預告期間：起~迄」時才記 comment_end，不自行推算日期。
- fetch 不發額外請求：RSS item 即為 raw 內容。
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import feedparser

from adapters.base import ItemRef, RawDoc, SourceAdapter
from adapters.common import TPE, html_to_text
from core.storage import utcnow

BASE = "https://www.fsc.gov.tw/ch/"
INSURANCE_CAKE = "490"


def canonical_draft_url(dataserno: str) -> str:
    return (f"{BASE}home.jsp?id=133&parentpath=0,3&mcustomize=lawnotice_view.jsp"
            f"&dataserno={dataserno}&dtable=NoticeLaw")


def _dataserno(link: str) -> str:
    q = parse_qs(urlsplit(html.unescape(link)).query)
    return (q.get("dataserno") or [""])[0]


_PERIOD = re.compile(r"-*\s*預告期間[:：]\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s*[~～-]\s*(\d{4})\.(\d{1,2})\.(\d{1,2})")
_TITLE_DOC_NO = re.compile(r"[（(]([^（()）]*字第[^（()）]*號)[)）]\s*$")
_ISSUED = re.compile(r"發文日期[:：]\s*中華民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DOC_NO = re.compile(r"發文字號[:：]\s*([^\s<]+號)")
_SUBJECT = re.compile(r"主旨[:：]\s*(.+)")
_UNDERTAKE = re.compile(r"承辦單位[:：]\s*([^。\n]+)")
_DAYS = re.compile(r"翌日起\s*([\d一二三四五六七八九十百]{1,4})\s*日內")
_ZH_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}


def zh_number(s: str) -> int | None:
    """「60」「六十」「三十」「十四」→ int（陳述意見期限有時寫國字）。"""
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _ZH_DIGITS:
            cur = _ZH_DIGITS[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            return None
    return total + cur or None


def clean_title(title: str) -> tuple[str, dict[str, Any]]:
    """標題常帶「(字號)--預告期間：起~迄」尾巴；拆出來，標題只留草案名稱。"""
    out: dict[str, Any] = {}
    t = (title or "").strip()
    if m := _PERIOD.search(t):
        y1, m1, d1, y2, m2, d2 = map(int, m.groups())
        out["comment_start"] = date(y1, m1, d1).isoformat()
        out["comment_end"] = date(y2, m2, d2).isoformat()
        t = t[:m.start()].rstrip()
    if m := _TITLE_DOC_NO.search(t):
        out["title_doc_no"] = m.group(1)
        t = t[:m.start()].rstrip()
    return t, out


def parse_draft_fields(title: str, description_html: str, categories: dict[str, str]) -> dict[str, Any]:
    text = html_to_text(description_html)
    clean, meta = clean_title(title)
    meta["clean_title"] = clean
    if m := _ISSUED.search(text):
        y, mo, d = map(int, m.groups())
        meta["issued_date"] = date(y + 1911, mo, d).isoformat()   # 民國年
    if m := _DOC_NO.search(text):
        meta["doc_no"] = m.group(1)
    elif meta.get("title_doc_no"):
        meta["doc_no"] = meta["title_doc_no"]
    meta.pop("title_doc_no", None)
    if m := _SUBJECT.search(text):
        meta["subject"] = m.group(1).strip()
    if m := _UNDERTAKE.search(text):
        meta["undertake"] = m.group(1).strip()
    if (m := _DAYS.search(text)) and (n := zh_number(m.group(1))):
        meta["comment_days"] = n
    meta["cake"] = categories.get("cake")
    meta["is_insurance"] = (categories.get("cake") == INSURANCE_CAKE
                            or meta.get("doc_no", "").startswith("金管保")
                            or "保險局" in meta.get("undertake", ""))
    missing = [k for k in ("doc_no", "subject", "undertake") if k not in meta]
    if missing:
        meta["parse_warnings"] = [f"missing {k}" for k in missing]
    return meta


class FscDraftAdapter(SourceAdapter):
    source_id = "fsc_draft"
    tier = "A"
    domain = "www.fsc.gov.tw"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.feed_url: str = config["feed_url"]
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._entries: dict[str, dict[str, Any]] = {}

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
            key = canonical_draft_url(serno)
            pub = None
            if e.get("published_parsed"):
                y, mo, d = e.published_parsed[:3]  # 來源以 00:00 GMT 表示「當天」
                pub = datetime(y, mo, d, tzinfo=TPE)
            cats = {t.get("scheme") or "": (t.get("term") or "").strip() for t in e.get("tags") or []}
            entry = {
                "dataserno": serno,
                "title": (e.get("title") or "").strip(),
                "pubDate": e.get("published", ""),
                "link": key,
                "description": e.get("description", ""),
                "categories": {k: v for k, v in cats.items() if k},
            }
            self._entries[key] = entry
            clean, _ = clean_title(entry["title"])
            refs.append(ItemRef(self.source_id, key, key, clean or entry["title"] or None, pub))
        return refs

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        entry = self._entries.get(ref.item_key)
        if entry is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        data = json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        path, digest = self.ctx.raw.put(self.source_id, data, "json", now)
        fields = parse_draft_fields(entry["title"], entry["description"], entry["categories"])
        meta: dict[str, Any] = {
            "title": fields.pop("clean_title") or ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "dataserno": entry["dataserno"],
            **fields,
            "event_extra": {k: fields.get(k) for k in ("undertake", "is_insurance", "comment_days", "comment_end")},
        }
        return RawDoc(self.source_id, ref.item_key, ref.url, now, digest, "rss_item",
                      path, None, None, meta)
