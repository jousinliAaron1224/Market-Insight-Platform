"""三層變動偵測（Handbook「變動偵測與版本保存」），由便宜到昂貴：

1. 列表層：item_key 比對 seen_items。沒看過的才往下；看過的只有在
   「列表指紋」（標題／日期／URL）改變、或 refetch=True 時才往下（決策 2026-10-02）。
2. HTTP 層：送 If-None-Match / If-Modified-Since，304 不下載（adapter 會 raise NotModified）。
3. 內容層：比對 content_hash 與該 item_key 最新版本，相同則丟棄。

內容不同時：第一次見到 → version 1 + new_item；已有版本 → version+1 + doc_revised
（決策 2026-10-02：新聞／公告改版也發 doc_revised）。
任務可重跑：以 (source_id, item_key, content_hash) 為唯一鍵，不產生重複資料。
"""
from __future__ import annotations

import hashlib
import logging
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from core import events
from core.storage import Database, utcnow

log = logging.getLogger(__name__)


def list_fingerprint(ref: ItemRef) -> str:
    pub = ref.published_at.isoformat() if ref.published_at else ""
    return hashlib.sha256(f"{ref.title or ''}\x1f{pub}\x1f{ref.url}".encode("utf-8")).hexdigest()


def validator_lookup(db: Database):
    """給 AdapterContext 用：取最新版本的 ETag / Last-Modified 做 conditional GET。"""
    def _lookup(source_id: str, item_key: str) -> tuple[str | None, str | None]:
        row = db.latest_version(source_id, item_key)
        return (row["etag"], row["last_modified"]) if row else (None, None)
    return _lookup


@dataclass
class RunStats:
    listed: int = 0
    skipped_seen: int = 0       # 第 1 層擋下
    not_modified: int = 0       # 第 2 層擋下
    unchanged: int = 0          # 第 3 層擋下
    new: int = 0                # new_item + doc_revised
    new_items: int = 0
    revised: int = 0
    errors: int = 0
    launched: int = 0           # product_launched（商品列表類來源）
    discontinued: int = 0       # product_discontinued
    error_messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_source(adapter: SourceAdapter, db: Database, *, refetch: bool = False) -> RunStats:
    stats = RunStats()
    db.purge_expired_leads(utcnow())  # 過期的新聞導言（D16）
    run_id = db.start_run(adapter.source_id, utcnow())
    status = "ok"
    try:
        refs = adapter.list_items()
        stats.listed = len(refs)
        sync = getattr(adapter, "sync_listing", None)  # 商品列表：比對前後商品集合（Handbook 上架／停售）
        if sync:
            res = sync(db, refs)
            stats.launched, stats.discontinued = res.get("launched", 0), res.get("discontinued", 0)
        for msg in getattr(adapter, "list_errors", []) or []:  # 多來源 adapter 的部分失敗
            stats.errors += 1
            stats.error_messages.append(f"list_items (partial): {msg}")
        for ref in refs:
            try:
                _process(adapter, db, ref, stats, refetch)
            except Exception as e:  # 單筆失敗不拖垮整輪；不寫 seen，下輪會再試
                db.conn.rollback()
                stats.errors += 1
                stats.error_messages.append(f"{ref.item_key}: {e!r}")
                log.warning("fetch failed %s: %s", ref.url, e)
    except Exception as e:  # 列表頁失敗＝整輪失敗，要大聲
        status = "error"
        stats.errors += 1
        stats.error_messages.append(f"list_items: {e!r}\n{traceback.format_exc(limit=3)}")
        log.error("list_items failed for %s: %s", adapter.source_id, e)
    finally:
        if stats.errors and status == "ok":
            status = "partial"
        db.finish_run(
            run_id,
            {"listed": stats.listed, "new": stats.new, "not_modified": stats.not_modified,
             "unchanged": stats.unchanged, "errors": stats.errors},
            status,
            "\n".join(stats.error_messages)[:4000] or None,
            utcnow(),
        )
    return stats


def _process(adapter: SourceAdapter, db: Database, ref: ItemRef, stats: RunStats, refetch: bool) -> None:
    now = utcnow()
    fp = list_fingerprint(ref)

    # ---- 第 1 層：列表 ----
    prev_fp = db.seen_fingerprint(ref.source_id, ref.item_key)
    if prev_fp is not None and prev_fp == fp and not refetch:
        stats.skipped_seen += 1
        db.touch_seen(ref.source_id, ref.item_key, now)
        db.conn.commit()
        return

    # ---- 第 2 層：HTTP conditional GET（在 adapter.fetch 內）----
    try:
        doc = adapter.fetch(ref)
    except NotModified:
        stats.not_modified += 1
        db.touch_seen(ref.source_id, ref.item_key, now, fp)
        db.conn.commit()
        return

    # 暫存文字（如新聞導言，D16）不得進入永久保存的 raw_docs.meta
    transient = doc.meta.pop("transient_lead", None)
    lead_ttl = int(doc.meta.pop("lead_ttl_days", 30))

    # ---- 第 3 層：內容 hash ----
    latest = db.latest_version(doc.source_id, doc.item_key)
    if latest is not None and latest["content_hash"] == doc.content_hash:
        if doc.raw_path != latest["raw_path"]:  # 指紋相同但原始位元組不同（D11）
            adapter.ctx.raw.discard_orphan(doc.raw_path, db)
        stats.unchanged += 1
        db.touch_seen(ref.source_id, ref.item_key, now, fp)
        db.conn.commit()
        return
    if latest is not None and (old := db.find_by_hash(doc.source_id, doc.item_key, doc.content_hash)):
        # 內容改回某個舊版本（A→B→A）。唯一鍵不允許重複存同一 hash，記為未變動並留 log。
        log.info("content reverted to older version: %s", doc.item_key)
        revert = getattr(adapter, "after_revert", None)  # 例如銀行上架：下架的商品又回到列表上
        if revert:
            revert(db, doc, int(old["id"]))
        stats.unchanged += 1
        db.touch_seen(ref.source_id, ref.item_key, now, fp)
        db.conn.commit()
        return

    raw_doc_id = _store_version(db, doc, latest)
    after = getattr(adapter, "after_store", None)  # 例如更新 products 表的最新條款版本
    if after:
        after(db, doc, raw_doc_id)
    if transient:
        db.put_lead(doc.source_id, doc.item_key, raw_doc_id, transient, now, lead_ttl)
    if latest is None:
        stats.new_items += 1
    else:
        stats.revised += 1
    stats.new += 1
    db.touch_seen(ref.source_id, ref.item_key, now, fp)
    db.conn.commit()


def _store_version(db: Database, doc: RawDoc, latest) -> int:
    version = 1 if latest is None else int(latest["version"]) + 1
    raw_doc_id = db.insert_raw_doc(doc, version)
    payload: dict[str, Any] = {
        "source_id": doc.source_id,
        "item_key": doc.item_key,
        "url": doc.url,
        "title": doc.meta.get("title"),
        "published_at": doc.meta.get("published_at"),
        "doc_type": doc.doc_type,
        "raw_path": doc.raw_path,
        "version": version,
    }
    payload.update(doc.meta.get("event_extra") or {})  # 來源特有、下游要過濾用的欄位
    if latest is None:
        events.emit(db, events.NEW_ITEM, raw_doc_id, payload)
    else:
        payload.update(previous_raw_doc_id=latest["id"], previous_version=latest["version"],
                       previous_raw_path=latest["raw_path"])
        if latest["url"] != doc.url:
            # 來源更正（D25）：同一項目改抓另一個網址（例如修正條款對應），不是原文件改版；
            # 照樣存新版本，但標記讓下游不要當成改版、不做逐條比對
            payload.update(url_changed=True, previous_url=latest["url"])
        events.emit(db, events.DOC_REVISED, raw_doc_id, payload)
    return raw_doc_id
