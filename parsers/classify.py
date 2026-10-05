"""新聞／法規分類（規則式，M4，D22–D23）。

類別：商品／利率／法規／通路／人事（可多類別）。標題命中 1 個關鍵字、或內文命中 2 個不同關鍵字即標該類別；
保發中心的法律命令／行政規則／行政函釋與金管會裁罰案一律帶「法規」。

影響程度（D23）：
- high：Cardif 主力險種／通路（投資型、變額、年金、利變、外幣保單、銀行保險）的法規、利率或商品消息；
        壽險同業被裁罰；資本制度（清償能力、TW-ICS、IFRS 17）；提到法巴
- low ：只關產險（車險、地震、旅平、登山…）、只有人事、非保險業、公關稿
- medium：其他
預設隱藏（hidden=1）：公關稿（D10）、金管會非保險單位的列表列（D12）、非保險業裁罰案。資料與標籤都保留。

每筆都記 reasons（命中的規則與關鍵字），可解釋、可調整；規則放在 sources.yaml 的 parsing.classify。
"""
from __future__ import annotations

import json
import re
from typing import Any

from adapters.common import html_to_text
from core import events
from core.storage import iso, utcnow
from parsers.base import ParseContext

CLASSIFIER = "rules-v1"
REG_DATA_TYPES = {"法律命令", "行政規則", "行政函釋"}
DEFAULT_SOURCES = ["tii_law_rss", "fsc_press", "fsc_penalty", "news_rss"]
BODY_CHARS = 4000


def document_text(ctx: ParseContext, doc, meta: dict[str, Any]) -> str:
    """分類用的正文：只在處理時讀 raw，不另外保存全文（新聞著作權，D16）。"""
    src, dtype = doc["source_id"], doc["doc_type"]
    try:
        if src == "tii_law_rss" and dtype == "html":
            from selectolax.parser import HTMLParser
            tree = HTMLParser(ctx.raw.read(doc["raw_path"]).decode("cp950", errors="replace"))
            pre = tree.css_first("table.news-table pre")
            return pre.text() if pre else ""
        if src == "fsc_press" and dtype == "html":
            from adapters.fsc_press import parse_detail
            return parse_detail(ctx.raw.read(doc["raw_path"]), doc["url"]).get("body_text", "")
        if src == "fsc_penalty":
            return html_to_text(json.loads(ctx.raw.read(doc["raw_path"])).get("description", ""))
        if src == "news_rss":
            return ctx.db.get_lead(src, doc["item_key"]) or ""   # 導言暫存表（30 天）
    except FileNotFoundError:
        return ""
    return ""


def _hits(patterns: list[str], text: str) -> list[str]:
    """關鍵字是正規表示式（可用 (?<!非) 排除「非投資型」這類情況），回傳實際命中的字。"""
    out = []
    for p in patterns:
        m = re.search(p, text)
        if m and m.group(0) not in out:
            out.append(m.group(0))
    return out


def classify(title: str, body: str, meta: dict[str, Any], source_id: str, rules: dict[str, Any]) -> dict[str, Any]:
    title = title or ""
    body = (body or "")[:BODY_CHARS]
    reasons: list[str] = []
    cats: list[str] = []
    for cat, kws in (rules.get("categories") or {}).items():
        t_hits = _hits(kws, title)
        b_hits = [k for k in _hits(kws, body) if k not in t_hits]
        if t_hits or len(b_hits) >= 2:
            cats.append(cat)
            reasons.append(f"{cat}: " + "、".join(t_hits + [f"{k}(內文)" for k in b_hits][:4]))
    if source_id == "tii_law_rss" and meta.get("data_type") in REG_DATA_TYPES and "法規" not in cats:
        cats.append("法規")
        reasons.append(f"法規: 保發中心資料類別 {meta['data_type']}")
    if source_id == "fsc_penalty" and "法規" not in cats:
        cats.append("法規")
        reasons.append("法規: 金管會裁罰案")
    order = list((rules.get("categories") or {}).keys())
    cats.sort(key=lambda c: order.index(c) if c in order else 99)

    def any_in(key: str, text: str) -> list[str]:
        return _hits(rules.get(key, []), text)

    hidden = False
    impact = "medium"
    if meta.get("pr_noise"):
        impact, hidden = "low", True
        reasons.append("low: 公關稿（D10）")
    elif source_id == "fsc_press" and meta.get("in_scope") is False:
        impact, hidden = "low", True
        reasons.append(f"low: 非保險單位（{meta.get('unit')}）")
    elif source_id == "fsc_penalty" and not meta.get("is_insurance"):
        impact, hidden = "low", True
        reasons.append("low: 非保險業裁罰")
    elif hits := any_in("self_names", title + body):
        impact = "high"
        reasons.append("high: 提到自家（" + "、".join(hits) + "）")
    elif hits := any_in("capital", title):
        impact = "high"
        reasons.append("high: 資本制度（" + "、".join(hits) + "）")
    elif source_id == "fsc_penalty" and "人壽" in (meta.get("respondent") or ""):
        impact = "high"
        reasons.append(f"high: 壽險同業裁罰（{meta.get('respondent')}）")
    elif re.search(r"裁罰|罰鍰|核處", title) and re.search(r"人壽", title):
        impact = "high"
        reasons.append("high: 壽險同業裁罰（新聞稿）")
    elif (hits := any_in("focus", title)) and set(cats) & {"法規", "利率", "商品"}:
        impact = "high"
        reasons.append("high: 主力險種／通路（" + "、".join(hits) + "）")
    elif (p := any_in("property_only", title)) and not any_in("life", title):
        impact = "low"
        reasons.append("low: 只關產險（" + "、".join(p) + "）")
    elif cats == ["人事"]:
        impact = "low"
        reasons.append("low: 人事")
    return {"categories": cats, "impact": impact, "hidden": hidden, "reasons": reasons}


class ClassifyHandler:
    name = "classify"

    def __init__(self, sources: list[str] | None = None):
        self.sources = sources

    def handles(self, event: dict[str, Any]) -> bool:
        pl = event["payload"]
        return (event["type"] in (events.NEW_ITEM, events.DOC_REVISED) and event.get("raw_doc_id") is not None
                and pl.get("source_id") in (self.sources or DEFAULT_SOURCES))

    def handle(self, ctx: ParseContext, event: dict[str, Any]) -> dict[str, Any]:
        doc = ctx.db.conn.execute("SELECT * FROM raw_docs WHERE id=?", (event["raw_doc_id"],)).fetchone()
        if doc is None:
            raise LookupError(f"raw_doc {event['raw_doc_id']} 不存在")
        meta = json.loads(doc["meta"])
        title = meta.get("detail_title") or meta.get("title") or ""
        body = document_text(ctx, doc, meta)
        labels = classify(title, body, meta, doc["source_id"], ctx.config.get("classify") or {})
        labels = ctx.enricher.refine_labels(f"{title}\n{body}", labels) or labels
        ctx.db.conn.execute(
            """INSERT OR REPLACE INTO doc_labels (raw_doc_id, source_id, item_key, title, published_at, categories,
                   impact, reasons, hidden, classifier, labeled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (doc["id"], doc["source_id"], doc["item_key"], meta.get("title"), meta.get("published_at"),
             json.dumps(labels["categories"], ensure_ascii=False), labels["impact"],
             json.dumps(labels["reasons"], ensure_ascii=False), int(labels["hidden"]),
             CLASSIFIER if ctx.enricher.name == "none" else f"{CLASSIFIER}+{ctx.enricher.name}", iso(utcnow())))
        return labels
