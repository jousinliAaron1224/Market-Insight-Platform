"""宣告利率（declared_rates，2026-10-05 新增）：各公司官網每月公告的利率變動型商品宣告利率。

每家公司一個 item（item_key = "rates:<company>"），fetch 依公司格式抓資料、正規化成：
  {code, name, month: YYYY-MM, rate_pct, currency, line}
只收名稱符合 name_filter 的商品（預設：美元＋利率變動），content_hash 用正規化資料列的指紋（D11）。
有新版本 → after_store 寫入 declared_rates 表（同一商品同一月份以最新公告為準）。

抓取量（同網域間隔 >= 3 秒）：
- 國泰、保誠：一個請求就有完整歷史
- 凱基、台灣人壽、南山：按月份／分頁查詢。第一次（資料庫還沒有這家公司的利率）回補 backfill_months 個月，
  之後每次只抓最近 recent_months 個月。凱基的歷史用每張商品的歷史 API。
各公司 2026-10-05 實際確認：robots.txt 未禁止這些 API、無機器人防護、TLS 正常。
未收：安聯（Cloudflare 驗證，不繞過）、友邦（連線逾時）、台新（2026-01 併入新光，新來源待確認）、
富邦（HTML 分頁量大）、安達（每張商品一個 PDF）、法巴（官網只公告貨幣帳戶與萬能利率，沒有利變商品）。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from selectolax.parser import HTMLParser

from adapters.base import ItemRef, RawDoc, SourceAdapter
from adapters.common import TPE, content_fingerprint
from core.storage import iso, utcnow


def parse_rate(v: Any) -> float | None:
    """'3.35%'、'4.20'、3.15 → float；'-'、'--'、'－'、空白 → None。"""
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    s = str(v or "").strip().rstrip("%").strip()
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def ym(year: int | str, month: int | str, roc: bool = False) -> str:
    y = int(year) + (1911 if roc else 0)
    return f"{y}-{int(month):02d}"


def slash_ym(s: str, roc: bool = False) -> str | None:
    """'2026/10' 或民國 '115/10' → '2026-10'。"""
    m = re.match(r"\s*(\d{2,4})\s*/\s*(\d{1,2})", s or "")
    return ym(m.group(1), m.group(2), roc) if m else None


def months_back(today: date, n: int) -> list[tuple[int, int]]:
    """含本月往回 n 個月：[(2026, 10), (2026, 9), …]。"""
    y, m, out = today.year, today.month, []
    for _ in range(max(1, n)):
        out.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


_WS = re.compile(r"\s+")
_CODE = re.compile(r"[【\[]([A-Za-z0-9]+)[】\]]")


def _row(code: str, name: str, month: str | None, rate: float | None) -> dict[str, Any] | None:
    name = _WS.sub("", name or "")
    if not month or rate is None or not name:
        return None
    cur = "USD" if "美元" in name else ("AUD" if "澳幣" in name else ("CNY" if "人民幣" in name else None))
    return {"code": str(code or name), "name": name, "month": month, "rate_pct": rate, "currency": cur,
            "line": "annuity" if "年金" in name else "usd_interest"}


class DeclaredRatesAdapter(SourceAdapter):
    source_id = "declared_rates"
    tier = "B"
    domain = "multiple"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.companies: list[dict[str, Any]] = config["companies"]
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self.name_filter = [re.compile(p) for p in config.get("name_filter", ["美元", "利率變動"])]
        self.recent_months = int(config.get("recent_months", 2))
        self.backfill_months = int(config.get("backfill_months", 12))
        self.keep_months = int(config.get("keep_months", 24))
        self.today = lambda: datetime.now(TPE).date()   # 測試可覆寫
        self._cfg: dict[str, dict[str, Any]] = {}
        self._has_history: set[str] = set()
        self._log: list[dict[str, Any]] = []

    # change_detect 在處理各 item 之前呼叫：用來判斷哪些公司還要回補歷史
    def sync_listing(self, db, refs) -> dict[str, int]:
        try:
            rows = db.conn.execute("SELECT company, COUNT(DISTINCT month) FROM declared_rates GROUP BY company").fetchall()
        except Exception:   # 舊資料庫還沒有這張表
            rows = []
        self._has_history = {r[0] for r in rows if r[1] >= min(6, self.backfill_months)}
        return {}

    def list_items(self) -> list[ItemRef]:
        """不發請求。published_at 用今天，讓列表指紋每天變一次 → 每次排程都會真的去抓。"""
        today = datetime.now(TPE).replace(hour=0, minute=0, second=0, microsecond=0)
        self._cfg.clear()
        refs = []
        for co in self.companies:
            if not co.get("enabled", True):
                continue
            key = f"rates:{co['id']}"
            self._cfg[key] = co
            refs.append(ItemRef(self.source_id, key, co["page_url"], f"{co['name']} 宣告利率", today))
        return refs

    def keep(self, name: str) -> bool:
        n = _WS.sub("", name or "")
        return all(p.search(n) for p in self.name_filter)

    # ---- HTTP（每個請求記下來，raw 存整包回應）----
    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> bytes:
        full = url if not params else f"{url}{'&' if '?' in url else '?'}" + "&".join(f"{k}={v}" for k, v in params.items())
        body = self.ctx.http.get(full, respect_robots=self.respect_robots, headers=headers).content
        self._log.append({"method": "GET", "url": full, "body": body.decode("utf-8", errors="replace")})
        return body

    def _post(self, url: str, *, data: dict | None = None, json_body: Any = None) -> bytes:
        body = self.ctx.http.post(url, data=data, json=json_body, respect_robots=self.respect_robots).content
        self._log.append({"method": "POST", "url": url, "form": data, "json": json_body,
                          "body": body.decode("utf-8", errors="replace")})
        return body

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        co = self._cfg.get(ref.item_key)
        if co is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        self._log = []
        backfill = co["id"] not in self._has_history
        months = months_back(self.today(), self.backfill_months if backfill else self.recent_months)
        rows = getattr(self, f"_fetch_{co['kind']}")(co, months)
        oldest = ym(*months_back(self.today(), self.keep_months)[-1])
        # 同一代碼同一月份可能出現兩次（凱基同一張商品同時列「凱基人壽…」與併購前的「中國人壽…」）：
        # 取名稱以目前公司名稱開頭的那筆
        picked: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            if not (r and self.keep(r["name"]) and r["month"] >= oldest):
                continue
            k = (r["code"], r["month"])
            if k not in picked or (r["name"].startswith(co["name"]) and not picked[k]["name"].startswith(co["name"])):
                picked[k] = r
        out = list(picked.values())
        if not out:
            raise ValueError(f"{ref.item_key} 解析到 0 筆宣告利率：網站可能改版")
        out.sort(key=lambda r: (r["code"], r["month"]))
        data = json.dumps({"company": co["id"], "requests": self._log}, ensure_ascii=False).encode("utf-8")
        path, _ = self.ctx.raw.put(self.source_id, data, "json", now)
        meta = {"title": ref.title, "published_at": None, "company": co["id"], "backfill": backfill,
                "months": [ym(*m) for m in months], "count": len(out), "products": len({r["code"] for r in out}),
                "rows": out, "event_extra": {"company": co["id"], "count": len(out)}}
        return RawDoc(self.source_id, ref.item_key, ref.url, now, content_fingerprint(out), "json", path,
                      None, None, meta)

    def after_store(self, db, doc: RawDoc, raw_doc_id: int) -> None:
        co = doc.meta["company"]
        db.conn.executemany(
            """INSERT INTO declared_rates (company, product_code, product_name, month, rate_pct, currency, line,
                                           source_url, raw_doc_id) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company, product_code, month) DO UPDATE SET product_name=excluded.product_name,
                   rate_pct=excluded.rate_pct, currency=excluded.currency, line=excluded.line,
                   source_url=excluded.source_url, raw_doc_id=excluded.raw_doc_id""",
            [(co, r["code"], r["name"], r["month"], r["rate_pct"], r["currency"], r["line"], doc.url, raw_doc_id)
             for r in doc.meta["rows"]])

    after_revert = after_store

    # ---------------------------------------------------------------- 各公司
    def _fetch_cathay_all(self, co, months):
        """國泰：一個 GET 回傳全部商品、全部月份（約 7 MB，民國年）。"""
        data = json.loads(self._get(co["api_url"]))
        if data.get("returnCode") not in (0, "0"):
            raise ValueError(f"國泰 API returnCode={data.get('returnCode')}")
        return [_row(r.get("prod_id"), r.get("prod_name"), ym(r["declare_year"], r["declare_month"], roc=True),
                     parse_rate(r.get("rate_m"))) for r in data.get("data") or []
                if r.get("declare_year") and r.get("declare_month")]

    def _fetch_pca_html(self, co, months):
        """保誠：靜態頁，每年一個表格（宣告年/月｜宣告利率）。頁面目前只有一張商品，名稱與代碼取自設定。"""
        tree = HTMLParser(self._get(co["page_url"]).decode("utf-8", errors="replace"))
        if co["product_name"] not in _WS.sub("", tree.body.text() if tree.body else ""):
            raise ValueError(f"保誠頁面找不到商品「{co['product_name']}」：頁面可能改版或換商品")
        rows = []
        for tr in tree.css(".cmp-accordion__item table tr"):
            td = tr.css("td")
            if len(td) >= 2:
                rows.append(_row(co["product_code"], co["product_name"], slash_ym(td[0].text(strip=True)),
                                 parse_rate(td[1].text(strip=True))))
        return rows

    def _fetch_kgi_api(self, co, months):
        """凱基：本月清單（POST，關鍵字「美元」分頁）＋回補時每張商品的歷史 API。"""
        rows, listed, page, total = [], [], 1, 1
        while page <= min(total, int(co.get("max_pages", 20))):
            d = json.loads(self._post(co["list_url"], data={"isOIU": "false", "page": str(page), "searchTxt": "美元"}))
            total = int(d.get("PageTotal") or 1)
            for r in d.get("InterestData") or []:
                rows.append(_row(r.get("PlanCode"), r.get("PlanName"), slash_ym(r.get("DecalreYM")), parse_rate(r.get("InterestRate"))))
                if self.keep(r.get("PlanName") or ""):
                    listed.append(r)
            page += 1
        if len(months) > self.recent_months:   # 回補：每張商品一次歷史查詢
            start, end = months[-1], months[0]
            for r in listed:
                d = json.loads(self._post(co["history_url"], data={
                    "isModalInit": "false", "page": "1", "planCode": r["PlanCode"], "prodType": r.get("ProdType") or "",
                    "StartDate": f"{start[0]}/{start[1]}/1", "EndDate": f"{end[0]}/{end[1]}/1"}))
                rows += [_row(h.get("PlanCode"), h.get("PlanName"), slash_ym(h.get("DecalreYM")), parse_rate(h.get("InterestRate")))
                         for h in d.get("InterestData") or []]
        return rows

    def _fetch_taiwanlife_api(self, co, months):
        """台灣人壽：POST JSON，依 subtype（B 外幣利變年金、D 外幣利變壽險）× 年月 × 分頁。"""
        rows = []
        for sub in co.get("subtypes", ["B", "D"]):
            for (y, m) in months:
                page, total = 1, 1
                while page <= min(total, int(co.get("max_pages", 30))):
                    d = json.loads(self._post(co["api_url"], json_body={
                        "page_no": page, "rate_type": "A", "subtype": sub, "year": str(y), "month": str(m)}))
                    total = int((d.get("page_info") or {}).get("total_page_size") or 1)
                    rows += [_row(r.get("item_serno"), r.get("item_name"), slash_ym(r.get("date")), parse_rate(r.get("rate")))
                             for r in d.get("datas") or []]
                    page += 1
        return rows

    def _fetch_nanshan_api(self, co, months):
        """南山：GET（要帶 Content-Type: application/json，否則 406），依商品類型 × 民國年月 × 分頁（每頁 10 筆）。"""
        rows, hdr = [], {"Content-Type": "application/json"}
        for ptype in co.get("product_types", [14, 17]):
            for (y, m) in months:
                page, total = 1, 1
                while page <= min(total, int(co.get("max_pages", 40))):
                    d = json.loads(self._get(co["api_url"], {"productId": -1, "productType": ptype, "year": y - 1911,
                                                             "month": m, "page": page}, hdr))
                    total = int((d.get("page_Info") or {}).get("total_page_size") or 1)
                    for r in d.get("datas") or []:
                        code = _CODE.search(r.get("name") or "")
                        rows.append(_row(code.group(1) if code else r.get("name"), _CODE.sub("", r.get("name") or ""),
                                         slash_ym(r.get("date"), roc=True), parse_rate(r.get("rate"))))
                    page += 1
        return rows
