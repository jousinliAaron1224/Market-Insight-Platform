"""前端原型的本機伺服器（Handbook 第四／五層的雛形）：只用 Python 標準庫，唯讀讀取 SQLite。

    python -m web.server                       # 讀 data/intel.db（先跑 python -m scheduler.run --parse）
    python -m web.server --snapshot demo-1005b # 讀重播後的工作資料庫（data/replay/<快照>/），原文從快照讀
    python -m web.server --db 路徑 --port 8765

瀏覽器開 http://127.0.0.1:8765 。只綁本機位址，不對外開放。

頁面：/（情報牆）、/compare.html（競品比較）、/market.html（市場數據）。
API（全部 GET、回 JSON）：
    /api/meta                     資料庫概況、篩選選項
    /api/week?days=7              本週要注意：最近 N 天的高影響／影響自家商品的項目＋商品動態
    /api/labels?group=law|news&impact=high,medium&category=&source=&q=&from=&to=&hidden=0|1|all&sort=impact|date
                                  每筆附 product_impact（對現有商品的影響摘要，D27）
    /api/impact?raw_doc_id=       單筆的完整影響：自家受影響商品清單、各競品數量、建議檢視的條文
    /api/products?company=&line=&currency=TWD|FX&q=&status=&has_clause=1
    /api/product?company=&name=   單一商品：統一欄位＋出處＋待補欄位＋版本與改版差異
    /api/articles?raw_doc_id=     條款條文（條號、標題、頁碼、本文）
    /api/market/supply            市場數據・供給面（各家新核准／修正節奏、險種幣別結構、結論句）
    /api/market/demand            市場數據・需求面（全市場保費收入依險種、年增率、結論句；開放資料）
    /api/market/companies         市場數據・公司比較（壽險財務業務指標：保費變動、繼續率、費用率、ROE；開放資料 7191）
    /<raw_path>（raw/…）          原始檔（條款 PDF 可加 #page=N 直接跳頁）；限定在 raw 目錄內
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from core.config import load_config, resolve

STATIC = Path(__file__).with_name("static")
SOURCE_LABELS = {
    "tii_law_rss": "保發中心法規", "fsc_press": "金管會新聞稿", "fsc_penalty": "金管會裁罰",
    "news_rss": "新聞", "company_cardif_products": "法巴人壽", "company_cathay_products": "國泰人壽",
    "company_fubon_products": "富邦人壽", "company_taiwanlife_products": "台灣人壽",
    "company_kgi_products": "凱基人壽",
}
IMPACT_ORDER = "CASE impact WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
GROUPS = {"law": ["tii_law_rss", "fsc_press", "fsc_penalty"], "news": ["news_rss"]}
PRODUCT_EVENT_TYPES = ("product_launched", "product_discontinued", "doc_revised")


class Store:
    """唯讀查詢。每次查詢開新的連線（ThreadingHTTPServer 多執行緒）。"""

    def __init__(self, db_path: Path, data_dir: Path, impact_rules: dict | None = None):
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)            # raw_path（raw/…）相對於這個目錄
        self.impact_rules = impact_rules or {}    # sources.yaml 的 parsing.product_impact（D27）

    @contextmanager
    def conn(self):
        c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def _tables(self, c) -> set[str]:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # ------------------------------------------------------------ meta
    def meta(self) -> dict[str, Any]:
        with self.conn() as c:
            t = self._tables(c)
            parsed = "doc_labels" in t and c.execute("SELECT COUNT(*) FROM doc_labels").fetchone()[0] > 0
            out: dict[str, Any] = {
                "db": str(self.db_path), "parsed": parsed,
                "events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "pending": c.execute("SELECT COUNT(*) FROM events WHERE processed=0").fetchone()[0],
                "source_labels": SOURCE_LABELS,
            }
            if "doc_labels" in t:
                out["impact_counts"] = dict(c.execute(
                    "SELECT impact, COUNT(*) FROM doc_labels WHERE hidden=0 GROUP BY impact").fetchall())
                out["hidden_count"] = c.execute("SELECT COUNT(*) FROM doc_labels WHERE hidden=1").fetchone()[0]
                out["label_sources"] = [r[0] for r in c.execute(
                    "SELECT DISTINCT source_id FROM doc_labels ORDER BY source_id")]
                rng = c.execute("SELECT MIN(published_at), MAX(published_at) FROM doc_labels").fetchone()
                out["date_range"] = [(rng[0] or "")[:10], (rng[1] or "")[:10]]
            out["categories"] = ["商品", "利率", "法規", "通路", "人事"]
            if "product_terms" in t:
                out["companies"] = [r[0] for r in c.execute(
                    "SELECT company FROM product_terms GROUP BY company ORDER BY company")]
                out["lines"] = [r[0] for r in c.execute(
                    "SELECT line FROM product_terms WHERE line IS NOT NULL GROUP BY line ORDER BY line")]
                out["products"] = c.execute("SELECT COUNT(*) FROM product_terms").fetchone()[0]
            out["last_runs"] = [dict(r) for r in c.execute(
                """SELECT source_id, MAX(finished_at) AS last_success FROM crawl_runs
                   WHERE status='ok' GROUP BY source_id ORDER BY source_id""")]
        return out

    # ------------------------------------------------------------ 情報牆
    def labels(self, q: dict[str, str]) -> dict[str, Any]:
        where, args = ["1=1"], []
        impacts = [x for x in (q.get("impact") or "").split(",") if x in ("high", "medium", "low")]
        if impacts:
            where.append(f"l.impact IN ({','.join('?' * len(impacts))})")
            args += impacts
        if q.get("category"):
            where.append("l.categories LIKE ?")
            args.append(f'%"{q["category"]}"%')
        if q.get("source"):
            where.append("l.source_id = ?")
            args.append(q["source"])
        if q.get("group") in GROUPS:
            srcs = GROUPS[q["group"]]
            where.append(f"l.source_id IN ({','.join('?' * len(srcs))})")
            args += srcs
        if q.get("q"):
            where.append("l.title LIKE ?")
            args.append(f"%{q['q']}%")
        if q.get("from"):
            where.append("substr(l.published_at,1,10) >= ?")
            args.append(q["from"])
        if q.get("to"):
            where.append("substr(l.published_at,1,10) <= ?")
            args.append(q["to"])
        hidden = q.get("hidden", "0")
        if hidden in ("0", "1"):
            where.append("l.hidden = ?")
            args.append(int(hidden))
        order = "l.published_at DESC" if q.get("sort") == "date" else f"{IMPACT_ORDER}, l.published_at DESC"
        limit = max(1, min(int(q.get("limit") or 500), 2000))
        with self.conn() as c:
            if "doc_labels" not in self._tables(c):
                return {"total": 0, "items": []}
            sql = f"""SELECT l.*, r.url, r.raw_path, r.doc_type, r.meta FROM doc_labels l
                      JOIN raw_docs r ON r.id = l.raw_doc_id WHERE {' AND '.join(where)}"""
            total = c.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
            rows = c.execute(f"{sql} ORDER BY {order} LIMIT ?", args + [limit]).fetchall()
            products = self._products_for_impact(c)
        return {"total": total, "items": [self._label_item(r, products) for r in rows]}

    def _products_for_impact(self, c) -> list[dict[str, Any]]:
        if "product_terms" not in self._tables(c):
            return []
        return [dict(r) for r in c.execute("SELECT company, name, line, currency, status FROM product_terms")]

    def _label_item(self, r, products: list[dict[str, Any]], detail: bool = False) -> dict[str, Any]:
        from parsers.impact import compute_impact, summary_line
        meta = json.loads(r["meta"] or "{}")
        extra = {k: meta[k] for k in ("unit", "respondent", "fine_twd", "data_type", "feed", "doc_no")
                 if meta.get(k) not in (None, "")}
        imp = compute_impact(r["title"], r["source_id"], products, self.impact_rules)
        impact = {"summary": summary_line(imp), "self_count": len(imp["self"]),
                  "competitor_count": sum(imp["competitors"].values()), "scope": imp["scope"],
                  "untracked": imp["untracked"], "company_level": imp["company_level"], "articles": imp["articles"],
                  "reasons": imp["reasons"]}
        if detail:
            impact.update(self_products=imp["self"], self_lines=imp["self_lines"], competitors=imp["competitors"],
                          self_company=self.impact_rules.get("self_company"))
        return {
            "raw_doc_id": r["raw_doc_id"], "source_id": r["source_id"],
            "source": SOURCE_LABELS.get(r["source_id"], r["source_id"]),
            "group": next((g for g, s in GROUPS.items() if r["source_id"] in s), "other"),
            "title": r["title"], "date": (r["published_at"] or "")[:10],
            "categories": json.loads(r["categories"]), "impact": r["impact"],
            "reasons": json.loads(r["reasons"]), "hidden": bool(r["hidden"]),
            "url": r["url"], "raw": f"/{r['raw_path']}" if r["doc_type"] != "list_row" else None,
            "extra": extra, "product_impact": impact,
        }

    def impact_detail(self, raw_doc_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("""SELECT l.*, r.url, r.raw_path, r.doc_type, r.meta FROM doc_labels l
                             JOIN raw_docs r ON r.id = l.raw_doc_id WHERE l.raw_doc_id=?""", (raw_doc_id,)).fetchone()
            if r is None:
                return None
            return self._label_item(r, self._products_for_impact(c), detail=True)

    # ------------------------------------------------------------ 本週要注意
    def week(self, days: int = 7) -> dict[str, Any]:
        """最近 N 天（以資料庫最新一筆日期為準，快照或重播時也不會是空的；D27）。"""
        from datetime import date, timedelta
        days = max(1, min(days, 90))
        with self.conn() as c:
            t = self._tables(c)
            if "doc_labels" not in t:
                return {"as_of": None, "focus": [], "product_events": [], "counts": {}}
            ev_rows = c.execute(f"""SELECT type, payload, created_at FROM events
                                    WHERE type IN ({','.join('?' * len(PRODUCT_EVENT_TYPES))})""",
                                PRODUCT_EVENT_TYPES).fetchall()
            pevents = []
            for e in ev_rows:
                pl = json.loads(e["payload"])
                if e["type"] == "doc_revised" and not str(pl.get("source_id", "")).startswith("company_"):
                    continue
                pevents.append({"type": e["type"], "date": pl.get("replay_at") or e["created_at"][:10],
                                "company": pl.get("company"), "title": pl.get("title"),
                                "reconstructed": bool(pl.get("reconstructed")),
                                "source_corrected": bool(pl.get("url_changed"))})
            latest = c.execute("SELECT MAX(substr(published_at,1,10)) FROM doc_labels WHERE hidden=0").fetchone()[0]
            as_of = max([d for d in [latest] + [p["date"] for p in pevents] if d] or [date.today().isoformat()])
            start = (date.fromisoformat(as_of) - timedelta(days=days - 1)).isoformat()
            rows = c.execute(f"""SELECT l.*, r.url, r.raw_path, r.doc_type, r.meta FROM doc_labels l
                                 JOIN raw_docs r ON r.id = l.raw_doc_id
                                 WHERE l.hidden=0 AND substr(l.published_at,1,10) BETWEEN ? AND ?
                                 ORDER BY {IMPACT_ORDER}, l.published_at DESC""", (start, as_of)).fetchall()
            products = self._products_for_impact(c)
        items = [self._label_item(r, products) for r in rows]
        # 本週要注意：高影響，或會影響自家商品的項目
        focus = [i for i in items if i["impact"] == "high" or i["product_impact"]["self_count"]]
        counts = {g: {"total": 0, "high": 0} for g in GROUPS}
        for i in items:
            if i["group"] in counts:
                counts[i["group"]]["total"] += 1
                counts[i["group"]]["high"] += i["impact"] == "high"
        pev = sorted((p for p in pevents if start <= p["date"] <= as_of), key=lambda p: p["date"], reverse=True)
        return {"as_of": as_of, "from": start, "days": days, "focus": focus, "counts": counts,
                "product_events": pev, "self_company": self.impact_rules.get("self_company")}

    # ------------------------------------------------------------ 競品比較
    def products(self, q: dict[str, str]) -> dict[str, Any]:
        where, args = ["1=1"], []
        for key in ("company", "line", "status"):
            if q.get(key):
                where.append(f"t.{key} = ?")
                args.append(q[key])
        if q.get("currency") == "TWD":
            where.append("t.currency = 'TWD'")
        elif q.get("currency") == "FX":
            where.append("t.currency <> 'TWD'")
        if q.get("q"):
            where.append("t.name LIKE ?")
            args.append(f"%{q['q']}%")
        if q.get("has_clause") == "1":
            where.append("t.clause_raw_doc_id IS NOT NULL")
        with self.conn() as c:
            if "product_terms" not in self._tables(c):
                return {"total": 0, "items": []}
            rows = c.execute(f"""
                SELECT t.*, d.missing, d.warnings, d.articles FROM product_terms t
                LEFT JOIN clause_docs d ON d.raw_doc_id = t.clause_raw_doc_id
                WHERE {' AND '.join(where)} ORDER BY t.company, t.name""", args).fetchall()
            # 條款解析過但被判定對應錯誤的商品，product_terms 沒有條款；從最新版本的 clause_docs 找警告
            warn = {}
            for w in c.execute("""SELECT company, product_name, warnings FROM clause_docs
                                  WHERE warnings LIKE '%不符%' OR warnings LIKE '%非主約%'"""):
                warn[(w["company"], w["product_name"])] = json.loads(w["warnings"])
        items = []
        for r in rows:
            warnings = json.loads(r["warnings"] or "[]") or warn.get((r["company"], r["name"]), [])
            items.append({
                "company": r["company"], "name": r["name"], "line": r["line"], "currency": r["currency"],
                "status": r["status"], "has_clause": r["clause_raw_doc_id"] is not None,
                "articles": r["articles"], "missing": json.loads(r["missing"] or "[]") if r["missing"] else None,
                "warnings": warnings,
            })
        return {"total": len(items), "items": items}

    def product(self, company: str, name: str) -> dict[str, Any] | None:
        with self.conn() as c:
            t = c.execute("SELECT * FROM product_terms WHERE company=? AND name=?", (company, name)).fetchone()
            if t is None:
                return None
            out: dict[str, Any] = {k: t[k] for k in t.keys()}
            for k in ("payment_modes", "coverage", "rate_terms", "filings"):
                out[k] = json.loads(out[k]) if out.get(k) else None
            rows = c.execute("""SELECT id, version, url, doc_type, raw_path, fetched_at, meta FROM raw_docs
                                WHERE source_id LIKE 'company_%' AND json_extract(meta, '$.title') = ?
                                  AND json_extract(meta, '$.company') = ? ORDER BY version""",
                             (name, company)).fetchall()
            versions = []
            for r in rows:
                cd = c.execute("SELECT warnings FROM clause_docs WHERE raw_doc_id=?", (r["id"],)).fetchone()
                diff = c.execute("SELECT * FROM clause_diffs WHERE raw_doc_id=?", (r["id"],)).fetchone()
                versions.append({
                    "raw_doc_id": r["id"], "version": r["version"], "url": r["url"], "doc_type": r["doc_type"],
                    "raw": f"/{r['raw_path']}" if r["doc_type"] == "pdf" else None,
                    "fetched_at": r["fetched_at"], "warnings": json.loads(cd["warnings"]) if cd else [],
                    "diff": None if diff is None else {k: json.loads(diff[k]) for k in ("added", "removed", "changed")},
                })
            out["versions"] = versions
            meta = json.loads(rows[-1]["meta"]) if rows else {}
            out["first_date"], out["latest_date"] = meta.get("first_date"), meta.get("latest_date")
            out["clause"] = None
            if t["clause_raw_doc_id"]:
                d = c.execute("SELECT * FROM clause_docs WHERE raw_doc_id=?", (t["clause_raw_doc_id"],)).fetchone()
                raw_path = c.execute("SELECT raw_path FROM raw_docs WHERE id=?",
                                     (t["clause_raw_doc_id"],)).fetchone()[0]
                if d is not None:
                    out["clause"] = {
                        "raw_doc_id": d["raw_doc_id"], "pages": d["pages"], "articles": d["articles"],
                        "fields": json.loads(d["fields"]), "evidence": json.loads(d["evidence"]),
                        "missing": json.loads(d["missing"]), "warnings": json.loads(d["warnings"]),
                        "parser": d["parser"], "raw": f"/{raw_path}",
                    }
        return out

    # ------------------------------------------------------------ 市場數據
    def market_supply(self) -> dict[str, Any]:
        from web.market import supply
        with self.conn() as c:
            if "product_terms" not in self._tables(c):
                return {"as_of": None, "companies": [], "conclusions": []}
            return supply(c, self.impact_rules.get("self_company"))

    def market_demand(self) -> dict[str, Any]:
        from web.market import demand
        with self.conn() as c:
            return demand(c, lambda rel: (self.data_dir / rel).read_bytes())

    def market_companies(self) -> dict[str, Any]:
        from web.market import companies
        with self.conn() as c:
            return companies(c, lambda rel: (self.data_dir / rel).read_bytes(), self.impact_rules.get("self_company"))

    def articles(self, raw_doc_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT seq, kind, article_no, title, text, page_start, page_end FROM clause_articles
                   WHERE raw_doc_id=? ORDER BY seq""", (raw_doc_id,))]

    # ------------------------------------------------------------ 原文
    def raw_file(self, rel: str) -> Path | None:
        rel = unquote(rel)
        if not rel.startswith("raw/"):
            return None
        root = (self.data_dir / "raw").resolve()
        p = (self.data_dir / rel).resolve()
        if root not in p.parents or not p.is_file():   # 防止 ../ 跳出 raw 目錄
            return None
        return p


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InsuranceIntelPrototype/0.1"

        def log_message(self, fmt, *args):          # 安靜一點，只記錯誤
            if args and str(args[1]).startswith(("4", "5")):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            u = urlsplit(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/meta":
                    return self._json(store.meta())
                if u.path == "/api/labels":
                    return self._json(store.labels(q))
                if u.path == "/api/week":
                    return self._json(store.week(int(q.get("days") or 7)))
                if u.path == "/api/impact":
                    d = store.impact_detail(int(q.get("raw_doc_id", "0")))
                    return self._json(d) if d else self._json({"error": "not found"}, 404)
                if u.path == "/api/market/supply":
                    return self._json(store.market_supply())
                if u.path == "/api/market/demand":
                    return self._json(store.market_demand())
                if u.path == "/api/market/companies":
                    return self._json(store.market_companies())
                if u.path == "/api/products":
                    return self._json(store.products(q))
                if u.path == "/api/product":
                    p = store.product(q.get("company", ""), q.get("name", ""))
                    return self._json(p) if p else self._json({"error": "not found"}, 404)
                if u.path == "/api/articles":
                    return self._json(store.articles(int(q.get("raw_doc_id", "0"))))
                if u.path.startswith("/raw/"):
                    p = store.raw_file(u.path[1:])          # /raw/xxx → raw/xxx（與 raw_path 相同）
                    if p is None:
                        return self._json({"error": "not found"}, 404)
                    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    if p.suffix == ".html":         # 保發中心內文是 Big5，其餘 UTF-8
                        ctype = "text/html; charset=" + ("big5" if "/tii_law_rss/" in str(p) else "utf-8")
                    elif p.suffix == ".json":
                        ctype = "application/json; charset=utf-8"
                    return self._send(200, p.read_bytes(), ctype)
                name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
                f = (STATIC / name).resolve()
                if STATIC.resolve() in f.parents and f.is_file():
                    ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
                    if ctype.startswith("text/") or ctype.endswith("javascript"):
                        ctype += "; charset=utf-8"
                    return self._send(200, f.read_bytes(), ctype)
                return self._json({"error": "not found"}, 404)
            except (ValueError, sqlite3.Error) as e:
                return self._json({"error": repr(e)}, 400)

    return Handler


def build_store(cfg: dict, db: str | None = None, snapshot: str | None = None) -> Store:
    data_dir = resolve(cfg["defaults"]["raw_root"]).parent
    db_path = resolve(cfg["defaults"]["db_path"])
    if snapshot:
        from demo.snapshot import resolve_snapshot
        snap = resolve_snapshot(snapshot, cfg)
        work = resolve(cfg["defaults"]["db_path"]).parent / "replay" / snap.name / "intel.db"
        db_path = work if work.is_file() else snap / "intel.db"
        data_dir = snap
    if db:
        db_path = Path(db)
    return Store(db_path, data_dir, (cfg.get("parsing") or {}).get("product_impact"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="前端原型（本機）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/intel.db）")
    ap.add_argument("--snapshot", help="讀重播工作資料庫 data/replay/<快照>/，原文從快照讀")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    store = build_store(load_config(a.config), a.db, a.snapshot)
    if not store.db_path.is_file():
        print(f"找不到資料庫：{store.db_path}")
        return 1
    m = store.meta()
    print(f"資料庫：{store.db_path}")
    if not m["parsed"]:
        print("⚠ 這個資料庫還沒解析（doc_labels 是空的）。先跑：python -m scheduler.run --parse")
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(store))
    print(f"開啟 http://127.0.0.1:{a.port}  （Ctrl+C 結束）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
