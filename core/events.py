"""事件流：下游只訂閱事件，不直接掃資料表（Handbook「變動偵測與版本保存」）。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.storage import Database, iso, utcnow

NEW_ITEM = "new_item"
DOC_REVISED = "doc_revised"
PRODUCT_LAUNCHED = "product_launched"
PRODUCT_DISCONTINUED = "product_discontinued"
EVENT_TYPES = {NEW_ITEM, DOC_REVISED, PRODUCT_LAUNCHED, PRODUCT_DISCONTINUED}


def emit(db: Database, type_: str, raw_doc_id: int | None, payload: dict[str, Any],
         now: datetime | None = None) -> int:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_}")
    cur = db.conn.execute(
        "INSERT INTO events (type, raw_doc_id, payload, created_at) VALUES (?, ?, ?, ?)",
        (type_, raw_doc_id, json.dumps(payload, ensure_ascii=False, default=str), iso(now or utcnow())),
    )
    return int(cur.lastrowid)


def pending(db: Database, types: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """取未處理事件（依 id 順序），供下游消費者使用。"""
    sql = "SELECT * FROM events WHERE processed=0"
    args: list[Any] = []
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        args.extend(sorted(types))
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    rows = db.conn.execute(sql, args).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def mark_processed(db: Database, event_ids: list[int]) -> None:
    db.conn.executemany("UPDATE events SET processed=1 WHERE id=?", [(i,) for i in event_ids])
    db.conn.commit()
