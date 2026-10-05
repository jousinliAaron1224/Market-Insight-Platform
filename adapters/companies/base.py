"""各壽險公司投資型商品 adapter 的共用骨架（M3，決策 D19–D21）。

資料流（每家公司一個子類別，只需提供網址與條款頁的格式）：
1. list_items：下載法定公開的「保險商品名稱、日期及文號」PDF（原檔存 raw），解析出投資型主約
   （名稱含「變額」或「投資型」，排除批註條款／附約），再從「契約條款」頁對應出每個商品的條款 PDF。
2. sync_listing（change_detect 呼叫）：與 products 表比對前後兩次商品集合，
   新出現 → product_launched，消失 → product_discontinued（D20）。
3. fetch：下載單一商品的條款 PDF 存 raw；內容改變時 change_detect 會新增版本並發 doc_revised（條款改版）。
4. after_store（change_detect 呼叫）：更新 products 表的最新條款版本。

ItemRef.published_at 用「最近一次核准／修正日期」，所以商品修正時列表指紋會變、自動重抓條款（D4）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from adapters.common import TPE
from adapters.companies import clause_index
from adapters.companies.disclosure_pdf import ProductRow, parse_disclosure_pdf
from core import events
from core.storage import Database, iso, utcnow

ANNUITY = re.compile(r"年金")
UNIVERSAL = re.compile(r"萬能")


def product_line(name: str) -> str:
    if ANNUITY.search(name):
        return "變額年金保險"
    if UNIVERSAL.search(name):
        return "變額萬能壽險"
    return "變額壽險"


def product_currency(name: str) -> str:
    for key, code in (("美元", "USD"), ("澳幣", "AUD"), ("人民幣", "CNY"), ("外幣", "FX")):
        if key in name:
            return code
    return "TWD"


class CompanyProductsAdapter(SourceAdapter):
    """子類別需設定 company（中文全名）與 name_prefix，並實作 clause_index_from(resp)。"""

    company: str = ""
    name_prefix: str = ""
    tier = "C"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.product_list_url: str = config["product_list_url"]
        self.product_list_page: str | None = config.get("product_list_page")       # 清單網址會變時，從此頁找
        self.product_list_link: str = config.get("product_list_link", r"商品.*(名稱|文號)")
        self.clause_index_url: str | None = config.get("clause_index_url")
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self.min_keep_ratio: float = float(config.get("discontinue_guard_ratio", 0.5))
        self._rows: dict[str, ProductRow] = {}
        self._clauses: dict[str, str] = {}
        self.list_meta: dict[str, Any] = {}
        self.list_errors: list[str] = []

    # ---------- 子類別可覆寫 ----------
    def clause_index_from(self, content: bytes, url: str) -> dict[str, str]:
        return clause_index.from_html(content, url)

    # ---------- 列表 ----------
    def key_for(self, name: str) -> str:
        return f"{self.source_id}:{clause_index.norm_name(name)}"

    def resolve_list_url(self) -> str:
        """有些公司每次更新清單會換檔名（例如富邦的 /cms/…/2026-09/xxx.pdf），從公開資訊頁找最新連結。"""
        if not self.product_list_page:
            return self.product_list_url
        resp = self.ctx.http.get(self.product_list_page, respect_robots=self.respect_robots)
        idx = clause_index.from_html(resp.content, self.product_list_page, pattern=self.product_list_link)
        return next(iter(idx.values()), self.product_list_url)

    def list_items(self) -> list[ItemRef]:
        now = utcnow()
        self.list_errors = []
        url = self.resolve_list_url()
        resp = self.ctx.http.get(url, respect_robots=self.respect_robots)
        list_path, list_hash = self.ctx.raw.put(self.source_id, resp.content, "pdf", now)
        rows = parse_disclosure_pdf(resp.content, self.name_prefix or None)
        products = [r for r in rows if r.is_investment and not r.is_rider]
        if not products:
            raise ValueError(f"{self.source_id}: 商品清單解析出 0 筆投資型商品，可能改版")
        self.list_meta = {"url": url, "raw_path": list_path, "content_hash": list_hash,
                          "all_rows": len(rows), "investment_products": len(products)}

        self._clauses = {}
        if self.clause_index_url:
            try:
                c = self.ctx.http.get(self.clause_index_url, respect_robots=self.respect_robots)
                self._clauses = self.clause_index_from(c.content, self.clause_index_url)
            except Exception as e:  # 條款頁失敗時仍可更新商品集合，只是本輪不抓條款
                self.list_errors.append(f"clause index: {e!r}")

        self._rows = {}
        refs = []
        for r in products:
            key = self.key_for(r.name)
            self._rows[key] = r
            clause = clause_index.match(r.name, self._clauses)
            latest = datetime.fromisoformat(r.latest_date).replace(tzinfo=TPE) if r.latest_date else None
            refs.append(ItemRef(self.source_id, key, clause or url, r.name, latest))
        return refs

    # ---------- 上架／停售（change_detect 於 list_items 後呼叫） ----------
    def sync_listing(self, db: Database, refs: list[ItemRef]) -> dict[str, int]:
        now = iso(utcnow())
        current = {self._rows[r.item_key].name: r for r in refs}
        prev = {row["name"]: row for row in db.conn.execute(
            "SELECT name, status FROM products WHERE company=?", (self.company,))}
        on_sale_before = {n for n, row in prev.items() if row["status"] == "on_sale"}
        baseline = not prev  # 第一次執行只建立基準，不發上架事件（D20）
        launched = [n for n in current if n not in on_sale_before]
        gone = [n for n in on_sale_before if n not in current]

        # 防呆：清單筆數驟降（例如 PDF 版面改變導致解析失敗）時，不判定停售，改發警示
        guard = bool(on_sale_before) and len(current) < len(on_sale_before) * self.min_keep_ratio
        if guard:
            self.list_errors.append(
                f"商品數由 {len(on_sale_before)} 降到 {len(current)}（< {self.min_keep_ratio:.0%}），"
                "暫不判定停售，請人工確認是否網站改版")
            gone = []

        base_payload = {"source_id": self.source_id, "company": self.company,
                        "list_url": self.list_meta.get("url"), "list_raw_path": self.list_meta.get("raw_path")}
        for name in launched:
            row = self._rows[current[name].item_key]
            db.conn.execute(
                """INSERT INTO products (company, name, line, currency, status, updated_at)
                   VALUES (?, ?, ?, ?, 'on_sale', ?)
                   ON CONFLICT(company, name) DO UPDATE SET status='on_sale', updated_at=excluded.updated_at""",
                (self.company, name, product_line(name), product_currency(name), now))
            if not baseline:
                events.emit(db, events.PRODUCT_LAUNCHED, None, {
                    **base_payload, "item_key": current[name].item_key, "title": name,
                    "line": product_line(name), "currency": product_currency(name),
                    "first_date": row.first_date, "latest_date": row.latest_date,
                    "relaunch": name in prev,
                })
        for name in gone:
            latest_id = db.conn.execute(
                "SELECT latest_raw_doc_id FROM products WHERE company=? AND name=?",
                (self.company, name)).fetchone()[0]
            db.conn.execute("UPDATE products SET status='discontinued', updated_at=? WHERE company=? AND name=?",
                            (now, self.company, name))
            events.emit(db, events.PRODUCT_DISCONTINUED, latest_id, {
                **base_payload, "item_key": self.key_for(name), "title": name,
                "line": product_line(name), "currency": product_currency(name),
            })
        db.conn.commit()
        return {"launched": 0 if baseline else len(launched), "discontinued": len(gone),
                "baseline": int(baseline), "guarded": int(guard)}

    # ---------- 單一商品條款 ----------
    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        row = self._rows.get(ref.item_key)
        if row is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        meta: dict[str, Any] = {
            "title": row.name,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "company": self.company,
            "line": product_line(row.name),
            "currency": product_currency(row.name),
            "first_date": row.first_date,
            "latest_date": row.latest_date,
            "doc_numbers": row.doc_numbers,
            "approvals": row.approvals,
            "list_raw_path": self.list_meta.get("raw_path"),
            "event_extra": {"company": self.company, "line": product_line(row.name)},
        }
        clause_url = clause_index.match(row.name, self._clauses)
        if not clause_url:  # 找不到條款連結：只記清單上的這一列（list_row）
            meta["parse_warnings"] = ["clause pdf not found"]
            data = json.dumps(row.as_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            path, digest = self.ctx.raw.put(self.source_id, data, "json", now)
            return RawDoc(self.source_id, ref.item_key, ref.url, now, digest, "list_row", path, None, None, meta)

        etag, last_mod = self.ctx.validators(self.source_id, ref.item_key)
        resp = self.ctx.http.get(clause_url, etag=etag, last_modified=last_mod,
                                 respect_robots=self.respect_robots)
        if resp.status_code == 304:
            raise NotModified(clause_url)
        if not resp.content.startswith(b"%PDF"):
            raise ValueError(f"條款不是 PDF（{resp.headers.get('Content-Type')}）：{clause_url}")
        path, digest = self.ctx.raw.put(self.source_id, resp.content, "pdf", now)
        meta["clause_url"] = clause_url
        return RawDoc(self.source_id, ref.item_key, clause_url, now, digest, "pdf", path,
                      resp.headers.get("ETag"), resp.headers.get("Last-Modified"), meta)

    # ---------- 版本寫入後（change_detect 呼叫） ----------
    def after_store(self, db: Database, doc: RawDoc, raw_doc_id: int) -> None:
        if doc.doc_type == "pdf":
            db.conn.execute("UPDATE products SET latest_raw_doc_id=?, updated_at=? WHERE company=? AND name=?",
                            (raw_doc_id, iso(utcnow()), self.company, doc.meta.get("title")))
