"""銀行通路上架（bank_shelf，2026-10-05 新增）：各銀行官網「代理銷售的保險商品」列表。

法巴的主力通路是銀行保險；競品在哪家銀行上架新商品、哪張商品下架，是通路與行銷最想知道的事。

每個列表頁一個 item（item_key = "<bank_id>:<page>"），fetch 抓整頁、解析成正規化的商品列：
  {insurer, product, category, currency, line}
content_hash 用正規化商品列的指紋（D11）：頁面上的快取參數、下載連結雜湊變了不算改版。
列表有變 → after_store 更新 bank_shelf 表（新上架寫 first_seen、消失寫 removed_at，不刪除）。

和商品清單一樣（D20）：
- 列表頁第一次抓到時只建立基準（baseline=1），不算新上架。
- 解析到 0 筆、或少於目前上架數的一半，視為網站改版或解析失敗：丟例外（整筆回滾、記錯誤、發警示），
  不把所有商品判成下架。

目前支援的銀行（2026-10-05 實際確認，robots.txt 都未禁止這些路徑、無機器人防護）：
- 兆豐 mega_api：商品頁背後的 JSON API（POST 表單），一次回傳整個上架清單、含正式商品名稱
- 華南 hncb_html：伺服器端產生的 HTML 卡片（.card-fund），保險公司與商品名稱分兩行
- 永豐 sinopac_html：商品櫥窗 HTML；項目帶 data-start-time／data-end-time，頁面用 JS 隱藏過期項目，
  所以這裡也照同樣的上下架時間過濾
不支援：中國信託（JS 機器人驗證，不繞過）、星展（網頁不列商品名稱）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from selectolax.parser import HTMLParser

from adapters.base import ItemRef, RawDoc, SourceAdapter
from adapters.common import TPE, content_fingerprint, node_text
from core.storage import iso, utcnow

# ---------------------------------------------------------------- 正規化

_INSURER_PREFIX = re.compile(r"^(?:法商)?(.{2,6}?人壽)")
_PAREN = re.compile(r"\s*[（(]原[:：][^)）]*[)）]")
_WS = re.compile(r"\s+")


def compat_cjk(s: str) -> str:
    """CJK 相容漢字（U+F900–U+FAFF）換回標準字：兆豐 API 偶爾用 U+F989「黎」，外觀相同但比對不到。

    只處理這個區段、不做整體 NFKC，避免把全形括號、斜線改掉而讓既有商品被誤判成下架再上架。
    """
    return "".join(unicodedata.normalize("NFKC", c) if "\uf900" <= c <= "\ufaff" else c for c in s or "")


def norm_insurer(name: str) -> str:
    """「法商法國巴黎人壽」「新光人壽 (原：台新人壽)」→「法國巴黎人壽」「新光人壽」。"""
    s = _PAREN.sub("", _WS.sub("", compat_cjk(name)))
    return s[2:] if s.startswith("法商") else s


def split_insurer(text: str) -> tuple[str | None, str]:
    """商品名稱前面常帶保險公司名稱：「友邦人壽鍾愛一生…」→（友邦人壽, 鍾愛一生…）。"""
    t = _WS.sub("", compat_cjk(text))
    m = _INSURER_PREFIX.match(t)
    if not m:
        return None, t
    return norm_insurer(m.group(0)), t[m.end():]


def strip_insurer(insurer: str, product: str) -> str:
    p = _WS.sub("", compat_cjk(product))
    for pre in (f"法商{insurer}", insurer):
        if p.startswith(pre):
            return p[len(pre):]
    return p


def line_of(product: str, category: str = "") -> str:
    """推估雷達的險種代碼；只看名稱與銀行分類，對不到就是 other。"""
    t = f"{product} {category}"
    if re.search(r"變額|投資型|investment", t):
        return "investment"
    if re.search(r"房貸|房屋|house", t):
        return "mortgage_term"
    if "分紅" in t:
        return "participating"
    if re.search(r"利率變動|利變", t) and re.search(r"美元|外幣|USD", t):
        return "usd_interest"
    if "年金" in t:
        return "annuity"
    if re.search(r"醫療|健康|防癌|癌症|重大|長照|長期照顧|失能|傷害|意外", t):
        return "health"
    return "other"


def _row(insurer: str, product: str, category: str = "", currency: str = "") -> dict[str, str]:
    insurer = norm_insurer(insurer)
    product = strip_insurer(insurer, product)
    return {"insurer": insurer, "product": product, "category": category.strip(),
            "currency": currency.strip(), "line": line_of(product, category)}


def _dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out, seen = [], set()
    for r in rows:
        k = (r["insurer"], r["product"])
        if r["insurer"] and r["product"] and k not in seen:
            seen.add(k)
            out.append(r)
    return sorted(out, key=lambda r: (r["insurer"], r["product"]))


# ---------------------------------------------------------------- 各銀行解析

def parse_mega(data: bytes) -> list[dict[str, str]]:
    items = json.loads(data)
    if not isinstance(items, list):
        raise ValueError(f"兆豐 API 格式改變：預期 list，實際 {type(items).__name__}")
    rows = [_row(x.get("InsuranceCompany") or "", x.get("InsuranceName") or "",
                 x.get("InsuranceType") or "", x.get("Currency") or "") for x in items]
    return _dedupe(rows)


def parse_hncb(data: bytes, category: str = "") -> list[dict[str, str]]:
    tree = HTMLParser(data.decode("utf-8", errors="replace"))
    rows = []
    for card in tree.css(".card-fund"):
        lines = node_text(card.css_first(".fund-title")).splitlines()
        if len(lines) < 2:
            continue
        specs = {}
        for li in card.css(".fund-list li"):
            k, v = li.css_first(".fund-spec"), li.css_first("strong")
            if k and v:
                specs[k.text(strip=True)] = v.text(strip=True)
        rows.append(_row(lines[0], "".join(lines[1:]), category, specs.get("幣別", "")))
    return _dedupe(rows)


def _window_ok(node, now: datetime) -> bool:
    """永豐的項目帶上下架時間，頁面用 JS 隱藏不在期間內的項目；這裡照做。"""
    def t(attr: str) -> datetime | None:
        v = (node.attributes.get(attr) or "")[:19]
        try:
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE)
        except ValueError:
            return None
    start, end = t("data-start-time"), t("data-end-time")
    return (start is None or start <= now) and (end is None or now <= end)


def parse_sinopac(data: bytes, now: datetime) -> list[dict[str, str]]:
    tree = HTMLParser(data.decode("utf-8", errors="replace"))
    rows = []
    for group in tree.css("li.itemListLi"):
        if not _window_ok(group, now):
            continue
        for sub in group.css("li.bmListLi"):
            if not _window_ok(sub, now):
                continue
            title = sub.css_first(".item_title p")
            category = title.text(strip=True) if title else ""
            for li in sub.css("ul.item_list > li"):
                p = li.css_first("p")
                insurer, product = split_insurer(p.text(strip=True) if p else "")
                if insurer:
                    rows.append(_row(insurer, product, category))
    return _dedupe(rows)


# ---------------------------------------------------------------- adapter

class BankShelfAdapter(SourceAdapter):
    source_id = "bank_shelf"
    tier = "C"
    domain = "multiple"   # 各銀行依自己的網域限速

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.banks: list[dict[str, Any]] = config["banks"]
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self.min_keep_ratio: float = float(config.get("min_keep_ratio", 0.5))
        self._pages: dict[str, dict[str, Any]] = {}

    def list_items(self) -> list[ItemRef]:
        """不發請求：每個設定好的列表頁就是一個 item。

        published_at 用今天（台北）讓列表指紋每天變一次，所以每天會真的抓一次；
        內容沒變時由第 3 層（正規化商品列的指紋）擋下，不會產生新版本。
        """
        today = datetime.now(TPE).replace(hour=0, minute=0, second=0, microsecond=0)
        self._pages.clear()
        refs = []
        for bank in self.banks:
            if not bank.get("enabled", True):
                continue
            for page in bank["pages"]:
                key = f"{bank['id']}:{page['key']}"
                self._pages[key] = {"bank": bank, "page": page}
                refs.append(ItemRef(self.source_id, key, page["url"],
                                    f"{bank['name']} {page.get('label', page['key'])}", today))
        return refs

    def _download(self, kind: str, page: dict[str, Any]) -> bytes:
        if kind == "mega_api":
            resp = self.ctx.http.post(page["url"], data=page.get("form") or {}, respect_robots=self.respect_robots)
        else:
            resp = self.ctx.http.get(page["url"], respect_robots=self.respect_robots)
        return resp.content

    def parse(self, kind: str, data: bytes, page: dict[str, Any], now: datetime) -> list[dict[str, str]]:
        if kind == "mega_api":
            return parse_mega(data)
        if kind == "hncb_html":
            return parse_hncb(data, page.get("label", ""))
        if kind == "sinopac_html":
            return parse_sinopac(data, now)
        raise ValueError(f"不支援的銀行格式：{kind}")

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        cfg = self._pages.get(ref.item_key)
        if cfg is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        bank, page = cfg["bank"], cfg["page"]
        data = self._download(bank["kind"], page)
        rows = self.parse(bank["kind"], data, page, now.astimezone(TPE))
        if not rows:
            raise ValueError(f"{ref.item_key} 解析到 0 筆商品：網站可能改版，不更新上架狀態")
        path, _ = self.ctx.raw.put(self.source_id, data, "json" if bank["kind"] == "mega_api" else "html", now)
        meta: dict[str, Any] = {
            "title": ref.title, "published_at": None, "bank_id": bank["id"], "bank_name": bank["name"],
            "page": page["key"], "count": len(rows), "rows": rows,
            "event_extra": {"bank_id": bank["id"], "count": len(rows)},
        }
        return RawDoc(self.source_id, ref.item_key, ref.url, now, content_fingerprint(rows),
                      "json" if bank["kind"] == "mega_api" else "html", path, None, None, meta)

    # ---- 列表有變（新版本已寫入）→ 更新 bank_shelf ----
    def after_store(self, db, doc: RawDoc, raw_doc_id: int) -> None:
        bank_id, key, rows = doc.meta["bank_id"], doc.item_key, doc.meta["rows"]
        now = iso(doc.fetched_at)
        cur = {(r["insurer"], r["product"]): r for r in db.conn.execute(
            "SELECT insurer, product, removed_at FROM bank_shelf WHERE bank_id=? AND item_key=?", (bank_id, key))}
        on_shelf = sum(1 for r in cur.values() if r["removed_at"] is None)
        if on_shelf and len(rows) < on_shelf * self.min_keep_ratio:
            raise ValueError(f"{key} 只解析到 {len(rows)} 筆（目前上架 {on_shelf} 筆）：疑似網站改版，不判定下架")
        baseline = int(not cur)
        seen = set()
        for r in rows:
            k = (r["insurer"], r["product"])
            seen.add(k)
            if k in cur:
                db.conn.execute(
                    """UPDATE bank_shelf SET removed_at=NULL, line=?, category=?, currency=?, raw_doc_id=?
                       WHERE bank_id=? AND item_key=? AND insurer=? AND product=?""",
                    (r["line"], r["category"], r["currency"], raw_doc_id, bank_id, key, *k))
            else:
                db.conn.execute(
                    """INSERT INTO bank_shelf (bank_id, item_key, insurer, product, line, category, currency,
                                               first_seen, removed_at, baseline, raw_doc_id)
                       VALUES (?,?,?,?,?,?,?,?,NULL,?,?)""",
                    (bank_id, key, *k, r["line"], r["category"], r["currency"], now, baseline, raw_doc_id))
        for k, r in cur.items():
            if k not in seen and r["removed_at"] is None:
                db.conn.execute("UPDATE bank_shelf SET removed_at=? WHERE bank_id=? AND item_key=? AND insurer=? AND product=?",
                                (now, bank_id, key, *k))

    # 列表改回某個舊版本（A→B→A）：change_detect 不存新版本，但上架狀態仍要跟著目前的列表
    after_revert = after_store
