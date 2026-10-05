"""product_launched／product_discontinued → product_terms 的銷售狀態。"""
from __future__ import annotations

import json
from typing import Any

from core import events
from core.storage import iso, utcnow
from parsers.base import ParseContext


class ProductEventHandler:
    name = "product"

    def handles(self, event: dict[str, Any]) -> bool:
        return event["type"] in (events.PRODUCT_LAUNCHED, events.PRODUCT_DISCONTINUED)

    def handle(self, ctx: ParseContext, event: dict[str, Any]) -> dict[str, Any]:
        pl = event["payload"]
        status = "on_sale" if event["type"] == events.PRODUCT_LAUNCHED else "discontinued"
        ctx.db.conn.execute(
            """INSERT INTO product_terms (company, name, line, currency, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(company, name) DO UPDATE SET status=excluded.status,
                   updated_at=excluded.updated_at""",
            (pl.get("company"), pl.get("title"), pl.get("line"), pl.get("currency"), status, iso(utcnow())))
        return {"company": pl.get("company"), "status": status,
                "relaunch": bool(pl.get("relaunch")), "reconstructed": bool(pl.get("reconstructed"))}


def product_row(db, company: str, name: str) -> dict[str, Any] | None:
    r = db.conn.execute("SELECT * FROM product_terms WHERE company=? AND name=?", (company, name)).fetchone()
    if r is None:
        return None
    out = dict(r)
    for k in ("payment_modes", "coverage", "rate_terms", "filings"):
        if out.get(k):
            out[k] = json.loads(out[k])
    return out
