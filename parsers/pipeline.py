"""事件流 → 解析層（M4）。下游只訂閱事件，不直接掃資料表（Handbook）。

process_pending() 依 id 順序取未處理事件，交給第一個 handles() 為真的 handler：
- 成功 → event_processing.status=ok、events.processed=1
- 沒有 handler 要處理（例如非保險單位的列表列以外的類型）→ skipped、processed=1
- 例外 → status=error、attempts+1，事件維持未處理，下一輪重試；
  超過 max_attempts（預設 3）→ status=failed、processed=1，並發健康檢查警示（source_id=parser）。
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from core import events
from core.health import _raise, _resolve
from core.storage import iso, utcnow
from parsers.base import Handler, ParseContext

log = logging.getLogger(__name__)
PARSER_SOURCE = "parser"
PARSE_FAILED = "parse_failed"


def default_handlers(ctx: ParseContext | None = None) -> list[Handler]:
    from parsers.classify import ClassifyHandler
    from parsers.clause import ClauseHandler
    from parsers.products import ProductEventHandler
    sources = (ctx.config.get("classify_sources") if ctx else None) or None
    return [ClauseHandler(), ProductEventHandler(), ClassifyHandler(sources)]


def _attempts(ctx: ParseContext, event_id: int) -> int:
    row = ctx.db.conn.execute("SELECT attempts FROM event_processing WHERE event_id=?", (event_id,)).fetchone()
    return int(row[0]) if row else 0


def _record(ctx: ParseContext, event_id: int, status: str, handler: str | None,
            attempts: int, error: str | None) -> None:
    ctx.db.conn.execute(
        """INSERT INTO event_processing (event_id, status, handler, attempts, error, processed_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO UPDATE SET status=excluded.status, handler=excluded.handler,
               attempts=excluded.attempts, error=excluded.error, processed_at=excluded.processed_at""",
        (event_id, status, handler, attempts, error, iso(utcnow())))


def process_pending(ctx: ParseContext, handlers: list[Handler] | None = None, *,
                    limit: int | None = None, max_attempts: int | None = None,
                    on_event: Callable[[dict[str, Any], str, dict[str, Any]], None] | None = None
                    ) -> dict[str, int]:
    """處理所有（或前 limit 筆）未處理事件。on_event(event, status, summary) 供重播／CLI 顯示。"""
    handlers = handlers if handlers is not None else default_handlers(ctx)
    max_attempts = int(max_attempts or ctx.config.get("max_attempts", 3))
    stats = {"ok": 0, "skipped": 0, "error": 0, "failed": 0}
    ctx.db.conn.commit()  # handler 失敗時會 rollback，先確保之前的寫入（例如剛發出的事件）已落地
    batch = int(ctx.config.get("batch", 200))
    done = 0
    failed_this_run = False
    retried: set[int] = set()   # 本輪已失敗過的事件不在同一輪重試
    while limit is None or done < limit:
        n = batch if limit is None else min(batch, limit - done)
        pend = [e for e in events.pending(ctx.db, limit=n + len(retried)) if e["id"] not in retried][:n]
        if not pend:
            break
        for ev in pend:
            done += 1
            h = next((h for h in handlers if h.handles(ev)), None)
            if h is None:
                _record(ctx, ev["id"], "skipped", None, _attempts(ctx, ev["id"]), None)
                events.mark_processed(ctx.db, [ev["id"]])
                stats["skipped"] += 1
                if on_event:
                    on_event(ev, "skipped", {})
                continue
            try:
                summary = h.handle(ctx, ev) or {}
                _record(ctx, ev["id"], "ok", h.name, _attempts(ctx, ev["id"]) + 1, None)
                events.mark_processed(ctx.db, [ev["id"]])   # 內含 commit
                stats["ok"] += 1
                status = "ok"
            except Exception as e:  # 單一事件失敗不拖垮整批
                ctx.db.conn.rollback()
                att = _attempts(ctx, ev["id"]) + 1
                err = f"{e!r}\n{traceback.format_exc(limit=4)}"[:4000]
                summary = {"error": repr(e)}
                if att >= max_attempts:
                    _record(ctx, ev["id"], "failed", h.name, att, err)
                    events.mark_processed(ctx.db, [ev["id"]])
                    stats["failed"] += 1
                    status = "failed"
                    failed_this_run = True
                    _raise(ctx.db, PARSER_SOURCE, PARSE_FAILED,
                           f"event #{ev['id']} ({ev['type']} {ev['payload'].get('item_key')}) "
                           f"解析失敗 {att} 次：{e!r}")
                    log.warning("ALERT [parser] event #%s failed %d times: %s", ev["id"], att, e)
                else:
                    _record(ctx, ev["id"], "error", h.name, att, err)
                    ctx.db.conn.commit()
                    stats["error"] += 1
                    status = "error"
                    log.warning("parse error event #%s (attempt %d): %s", ev["id"], att, e)
                retried.add(ev["id"])
            if on_event:
                on_event(ev, status, summary)
            if limit is not None and done >= limit:
                break
    if not failed_this_run and stats["error"] == 0:
        _resolve(ctx.db, PARSER_SOURCE, PARSE_FAILED)  # 本輪全部成功 → 解除舊警示
    ctx.db.conn.commit()
    return stats
