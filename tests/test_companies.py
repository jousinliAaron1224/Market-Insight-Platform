"""M3：5 家壽險公司投資型商品 adapter。用 2026-10-03 實際下載的官網檔案當 fixture。"""
import json

import httpx
import pytest

from adapters.companies import clause_index
from adapters.companies.cardif import CardifProductsAdapter
from adapters.companies.cathay import CathayProductsAdapter
from adapters.companies.disclosure_pdf import parse_disclosure_pdf
from adapters.companies.fubon import FubonProductsAdapter
from adapters.companies.kgi import KgiProductsAdapter
from adapters.companies.taiwanlife import TaiwanLifeProductsAdapter
from core import events
from core.change_detect import run_source, validator_lookup
from core.config import load_config, source_config
from tests.conftest import FakeSite, fixture_bytes, make_ctx

CLAUSE_PDF = fixture_bytes("companies", "cardif", "clause_UA0020.pdf")

COMPANIES = {
    # source_id: (類別, fixture 目錄, {網址: fixture 檔}, 預期投資型商品數, 預期找到條款數)
    "company_cardif_products": (CardifProductsAdapter, "cardif", {"clause_index_url": "clause_page.html"}, 128, 120),
    "company_cathay_products": (CathayProductsAdapter, "cathay", {"clause_index_url": "public_info_page.html"}, 48, 48),
    "company_fubon_products": (FubonProductsAdapter, "fubon",
                               {"clause_index_url": "public_info_page.html", "product_list_page": "public_info_page.html"}, 20, 20),
    "company_taiwanlife_products": (TaiwanLifeProductsAdapter, "taiwanlife", {"clause_index_url": "clause_list.pdf"}, 41, 41),
    "company_kgi_products": (KgiProductsAdapter, "kgi", {"clause_index_url": "clause_page.html"}, 39, 39),
}


class CompanySite(FakeSite):
    """清單與條款頁用 fixture；其他 PDF 請求一律回條款範本（每個網址附上不同尾巴，讓雜湊不同）。"""

    def __init__(self, routes, clause_bytes=CLAUSE_PDF):
        super().__init__(routes)
        self.clause_bytes = clause_bytes
        self.overrides = {}

    def handler(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url in self.overrides:
            return httpx.Response(200, content=self.overrides[url])
        if url in self.routes:
            status, body, headers = self.routes[url]
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(200, content=self.clause_bytes + b"\n%" + url.encode())


def setup(make_env, sid, cfg_override=None):
    cls, folder, pages, _, _ = COMPANIES[sid]
    cfg = source_config(load_config(), sid)
    cfg.update(cfg_override or {})
    routes = {cfg["product_list_url"]: (200, fixture_bytes("companies", folder, "product_list.pdf"), {})}
    for key, fname in pages.items():
        routes[cfg[key]] = (200, fixture_bytes("companies", folder, fname), {})
    if "product_list_page" in cfg and "product_list_page" not in pages:
        cfg.pop("product_list_page")  # 沒有存公開資訊頁 fixture 的公司，直接用設定中的清單網址
    site = CompanySite(routes)
    db, raw, http = make_env(site)
    return site, db, raw, cls(make_ctx(raw, http, validator_lookup(db)), cfg)


@pytest.mark.parametrize("sid", list(COMPANIES))
def test_list_items_counts_and_clause_mapping(make_env, sid):
    site, db, raw, ad = setup(make_env, sid)
    refs = ad.list_items()
    _, _, _, n_products, n_clauses = COMPANIES[sid]
    assert len(refs) == n_products
    assert all(r.title.startswith(ad.name_prefix) and "變額" in r.title for r in refs)
    assert all(r.published_at is not None for r in refs)
    assert sum(r.url.endswith("product_list.pdf") is False and r.url != ad.list_meta["url"] for r in refs) == n_clauses
    assert ad.list_meta["raw_path"].endswith(".pdf")


def test_fubon_discovers_latest_list_url_from_page(make_env):
    site, db, raw, ad = setup(make_env, "company_fubon_products", {"product_list_url": "https://example.invalid/old.pdf"})
    # 頁面上的連結指向 2026-09 版清單；舊網址不應被請求
    cfg = source_config(load_config(), "company_fubon_products")
    site.routes[cfg["product_list_url"]] = (200, fixture_bytes("companies", "fubon", "product_list.pdf"), {})
    refs = ad.list_items()
    assert len(refs) == 20 and ad.list_meta["url"] == cfg["product_list_url"]
    assert site.hits("example.invalid") == 0


def test_first_run_is_baseline_then_launch_and_discontinue(make_env):
    site, db, raw, ad = setup(make_env, "company_kgi_products")
    st = run_source(ad, db)
    assert (st.new_items, st.launched, st.discontinued, st.errors) == (39, 0, 0, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM products WHERE company='凱基人壽' AND status='on_sale'").fetchone()[0] == 39
    assert db.conn.execute("SELECT COUNT(*) FROM products WHERE latest_raw_doc_id IS NOT NULL").fetchone()[0] == 39
    assert events.pending(db, {events.PRODUCT_LAUNCHED}) == []

    # 模擬上一輪的商品集合：少一個（→ 本輪視為新上架）、多一個已不在清單上的（→ 停售）
    gone_name = db.conn.execute("SELECT name FROM products LIMIT 1").fetchone()[0]
    db.conn.execute("DELETE FROM products WHERE name=?", (gone_name,))
    db.conn.execute("INSERT INTO products (company, name, status) VALUES ('凱基人壽', '凱基人壽舊商品變額壽險', 'on_sale')")
    db.conn.commit()
    st = run_source(ad, db)
    assert (st.launched, st.discontinued) == (1, 1)
    launched = events.pending(db, {events.PRODUCT_LAUNCHED})
    disc = events.pending(db, {events.PRODUCT_DISCONTINUED})
    assert launched[0]["payload"]["title"] == gone_name and launched[0]["payload"]["line"]
    assert disc[0]["payload"]["title"] == "凱基人壽舊商品變額壽險"
    status = db.conn.execute("SELECT status FROM products WHERE name='凱基人壽舊商品變額壽險'").fetchone()[0]
    assert status == "discontinued"


def test_guard_against_mass_discontinue(make_env):
    site, db, raw, ad = setup(make_env, "company_kgi_products")
    run_source(ad, db)
    for i in range(60):  # 上一輪「有」99 個商品，本輪只解析出 39 個（< 50%）
        db.conn.execute("INSERT INTO products (company, name, status) VALUES ('凱基人壽', ?, 'on_sale')", (f"凱基人壽假商品{i}變額壽險",))
    db.conn.commit()
    st = run_source(ad, db)
    assert st.discontinued == 0 and st.errors >= 1
    assert "暫不判定停售" in "\n".join(st.error_messages)
    assert events.pending(db, {events.PRODUCT_DISCONTINUED}) == []


def test_clause_revision_creates_version_and_updates_product(make_env):
    site, db, raw, ad = setup(make_env, "company_cathay_products")
    run_source(ad, db)
    refs = ad.list_items()
    target = refs[0]
    site.overrides[target.url] = CLAUSE_PDF + b"\n% revised clause"
    st = run_source(ad, db, refetch=True)
    assert st.revised == 1
    rev = events.pending(db, {events.DOC_REVISED})
    assert rev[0]["payload"]["company"] == "國泰人壽" and rev[0]["payload"]["previous_version"] == 1
    latest = db.conn.execute("SELECT latest_raw_doc_id FROM products WHERE name=?", (target.title,)).fetchone()[0]
    assert latest == rev[0]["raw_doc_id"]


def test_missing_clause_recorded_as_list_row_with_warning(make_env):
    site, db, raw, ad = setup(make_env, "company_cardif_products")
    st = run_source(ad, db)
    rows = db.conn.execute("SELECT meta FROM raw_docs WHERE doc_type='list_row'").fetchall()
    assert len(rows) == 8 and all("clause pdf not found" in json.loads(r["meta"])["parse_warnings"] for r in rows)
    assert st.errors == 0


def test_non_pdf_clause_is_an_error(make_env):
    site, db, raw, ad = setup(make_env, "company_kgi_products")
    site.clause_bytes = b"<html>Request Rejected</html>"
    st = run_source(ad, db)
    assert st.errors == 39 and st.new_items == 0


def test_parse_handles_kangxi_glyphs_and_dotted_dates():
    rows = parse_disclosure_pdf(fixture_bytes("companies", "cardif", "product_list.pdf"), "法商法國巴黎人壽")
    r = next(r for r in rows if r.name == "法商法國巴黎人壽滿鑫100變額萬能壽險")
    assert r.first_date == "2013-04-26" and "巴黎(102)壽字第04006號" in r.doc_numbers
    fub = parse_disclosure_pdf(fixture_bytes("companies", "fubon", "product_list.pdf"), "富邦人壽")
    assert all(x.name.startswith("富邦人壽") for x in fub)  # 跨頁斷開的名稱已併回


def test_clause_index_name_normalisation():
    idx = {"富邦人壽鑫享人生變額年金保險": "u1", "凱基人壽鑫旺九九變額壽險(112)": "u2"}
    assert clause_index.match("富邦人壽鑫享人生變額年金保險pdf", idx) == "u1"
    assert clause_index.match("凱基人壽鑫旺九九變額壽險", idx) == "u2"


def test_taiwanlife_clause_rows_not_merged_with_neighbours():
    """台灣人壽條款清單列距約 13.5pt：每個連結只能對到自己那一列，不能把上下列（常是批註條款）併進名稱。
    2026-10-04 前有 10 個商品因此被對應到「投資標的…批註條款」。"""
    idx = clause_index.from_pdf_hyperlinks(fixture_bytes("companies", "taiwanlife", "clause_list.pdf"))
    assert all(len(k) <= 30 for k in idx)
    rows = [r for r in parse_disclosure_pdf(fixture_bytes("companies", "taiwanlife", "product_list.pdf"), "台灣人壽")
            if r.is_investment and not r.is_rider]
    for r in rows:
        url = clause_index.match(r.name, idx)
        key = next(k for k, v in idx.items() if v == url)
        assert "批註" not in key and key.startswith(clause_index.norm_name(r.name)), (r.name, key)
    assert idx["台灣人壽鑫豐收外幣變額萬能壽險"].endswith("/File/10208")
