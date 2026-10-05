"""條款事件 handler：company_* 來源的 new_item／doc_revised → 條文、統一商品 schema、改版差異。

- pdf：parse_clause_pdf 切條文、抽欄位，寫 clause_docs／clause_articles，更新 product_terms。
- list_row（清單上有、但找不到條款連結的商品，D21）：product_terms 只填清單上的欄位，全部欄位列為 missing。
- doc_revised：另外和前一版逐條比對，寫 clause_diffs（前一版還沒解析過就先補解析）。
同一個 raw_doc 重跑會先刪掉舊結果再寫（冪等）。
"""
from __future__ import annotations

import json
from typing import Any

from core import events
from core.storage import iso, utcnow
from parsers.base import ParseContext
from parsers.clause_pdf import PARSER_NAME, REQUIRED, article_diff, parse_clause_pdf


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


class ClauseHandler:
    name = "clause"

    def handles(self, event: dict[str, Any]) -> bool:
        pl = event["payload"]
        return (event["type"] in (events.NEW_ITEM, events.DOC_REVISED)
                and str(pl.get("source_id", "")).startswith("company_")
                and pl.get("doc_type") in ("pdf", "list_row") and event.get("raw_doc_id") is not None)

    def handle(self, ctx: ParseContext, event: dict[str, Any]) -> dict[str, Any]:
        rid = int(event["raw_doc_id"])
        doc = ctx.db.conn.execute("SELECT * FROM raw_docs WHERE id=?", (rid,)).fetchone()
        if doc is None:
            raise LookupError(f"raw_doc {rid} 不存在")
        meta = json.loads(doc["meta"])
        if doc["doc_type"] == "list_row":
            self._upsert_terms(ctx, doc, meta, None)
            return {"articles": 0, "fields": 0, "missing": list(REQUIRED), "no_clause": True}

        parsed = ensure_parsed(ctx, rid)
        summary: dict[str, Any] = {"articles": parsed["articles"], "fields": len(parsed["fields"]),
                                   "missing": parsed["missing"], "warnings": parsed["warnings"]}
        rev = event["payload"].get("revision_date")
        if rev:  # 重播時重建的改版：附上同一天的文號（條款前言）
            summary["filings"] = [f for f in parsed["fields"].get("filings", []) if f.get("date") == rev]
        prev = event["payload"].get("previous_raw_doc_id")
        if event["payload"].get("url_changed"):
            summary["source_corrected"] = True          # D25：來源更正，不是改版，不比對前一版
        elif event["type"] == events.DOC_REVISED and prev:
            ensure_parsed(ctx, int(prev))
            d = article_diff(articles_of(ctx, int(prev)), articles_of(ctx, rid))
            ctx.db.conn.execute(
                """INSERT OR REPLACE INTO clause_diffs (raw_doc_id, previous_raw_doc_id, added, removed, changed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rid, int(prev), _j(d["added"]), _j(d["removed"]), _j(d["changed"]), iso(utcnow())))
            summary["diff"] = d
        if parsed["not_main_clause"]:
            summary["not_main_clause"] = True
        if self._is_latest(ctx, doc):   # 抓錯檔（批註條款）時不拿它的欄位當商品條款
            self._upsert_terms(ctx, doc, meta, None if parsed["not_main_clause"] else parsed)
        return summary

    @staticmethod
    def _is_latest(ctx: ParseContext, doc) -> bool:
        top = ctx.db.conn.execute("SELECT MAX(version) FROM raw_docs WHERE source_id=? AND item_key=?",
                                  (doc["source_id"], doc["item_key"])).fetchone()[0]
        return int(doc["version"]) >= int(top or 0)

    @staticmethod
    def _upsert_terms(ctx: ParseContext, doc, meta: dict[str, Any], parsed: dict[str, Any] | None) -> None:
        company, name = meta.get("company"), meta.get("title")
        st = ctx.db.conn.execute("SELECT status FROM products WHERE company=? AND name=?", (company, name)).fetchone()
        f = (parsed or {}).get("fields", {})
        ctx.db.conn.execute(
            """INSERT INTO product_terms (company, name, line, currency, status, issue_age, payment_modes, coverage,
                   exclusions, rate_terms, riders, clause_raw_doc_id, clause_version, clause_url, filings, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company, name) DO UPDATE SET line=excluded.line, currency=excluded.currency,
                   status=COALESCE(excluded.status, product_terms.status), issue_age=excluded.issue_age,
                   payment_modes=excluded.payment_modes, coverage=excluded.coverage, exclusions=excluded.exclusions,
                   rate_terms=excluded.rate_terms, riders=excluded.riders,
                   clause_raw_doc_id=excluded.clause_raw_doc_id, clause_version=excluded.clause_version,
                   clause_url=excluded.clause_url, filings=excluded.filings, updated_at=excluded.updated_at""",
            (company, name, meta.get("line"), f.get("currency") or meta.get("currency"),
             st["status"] if st else None, f.get("issue_age"), _j(f.get("payment_modes")), _j(f.get("coverage")),
             f.get("exclusions"), _j(f.get("rate_terms")), f.get("riders"),
             doc["id"] if parsed else None, doc["version"] if parsed else None,
             meta.get("clause_url") if parsed else None, _j(f.get("filings")), iso(utcnow())))


def articles_of(ctx: ParseContext, raw_doc_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in ctx.db.conn.execute(
        "SELECT * FROM clause_articles WHERE raw_doc_id=? ORDER BY seq", (raw_doc_id,))]


def ensure_parsed(ctx: ParseContext, raw_doc_id: int, force: bool = False) -> dict[str, Any]:
    """解析一個條款版本並寫入 clause_docs／clause_articles；已解析過就直接讀回（force 重做）。"""
    row = ctx.db.conn.execute("SELECT * FROM clause_docs WHERE raw_doc_id=?", (raw_doc_id,)).fetchone()
    if row is not None and not force:
        warnings = json.loads(row["warnings"])
        return {"articles": row["articles"], "fields": json.loads(row["fields"]),
                "missing": json.loads(row["missing"]), "warnings": warnings,
                "not_main_clause": any(w.startswith("非主約條款") for w in warnings)}
    doc = ctx.db.conn.execute("SELECT * FROM raw_docs WHERE id=?", (raw_doc_id,)).fetchone()
    meta = json.loads(doc["meta"])
    res = parse_clause_pdf(ctx.raw.read(doc["raw_path"]), meta.get("title"))
    rows = res.rows()
    fields, evidence, missing = dict(res.fields), dict(res.evidence), list(res.missing)

    # LLM 補強掛點（D22：預設 NullEnricher，不改任何東西）。只接受補 missing 的欄位，且必須附出處。
    extra = {} if res.not_main_clause else (ctx.enricher.enrich_clause(fields, missing, rows) or {})
    for k, v in extra.items():
        if k in missing and isinstance(v, dict) and "value" in v and v.get("evidence"):
            fields[k] = v["value"]
            evidence[k] = {**v["evidence"], "by": ctx.enricher.name}
            missing.remove(k)

    ctx.db.conn.execute("DELETE FROM clause_articles WHERE raw_doc_id=?", (raw_doc_id,))
    ctx.db.conn.executemany(
        """INSERT INTO clause_articles (raw_doc_id, seq, kind, article_no, title, text, page_start, page_end, text_hash)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(raw_doc_id, r["seq"], r["kind"], r["article_no"], r["title"], r["text"], r["page_start"],
          r["page_end"], r["text_hash"]) for r in rows])
    n_articles = len(res.articles)
    parser = PARSER_NAME if ctx.enricher.name == "none" else f"{PARSER_NAME}+{ctx.enricher.name}"
    ctx.db.conn.execute(
        """INSERT OR REPLACE INTO clause_docs (raw_doc_id, source_id, item_key, company, product_name, version, pages,
               articles, fields, evidence, missing, warnings, parser, parsed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (raw_doc_id, doc["source_id"], doc["item_key"], meta.get("company"), meta.get("title"), doc["version"],
         res.pages, n_articles, _j(fields), _j(evidence), _j(missing), _j(res.warnings), parser, iso(utcnow())))
    return {"articles": n_articles, "fields": fields, "missing": missing, "warnings": res.warnings,
            "not_main_clause": res.not_main_clause}
