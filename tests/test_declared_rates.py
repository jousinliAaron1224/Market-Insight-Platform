"""declared_rates（宣告利率）：fixture 是 2026-10-05 實際抓到的回應（國泰的 7 MB 全量資料只留 3 張商品）。"""
from datetime import date

import httpx
import pytest

from adapters.declared_rates import DeclaredRatesAdapter, months_back, parse_rate, slash_ym
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, fixture_bytes, make_ctx

FX = lambda name: fixture_bytes("declared_rates", name)  # noqa: E731


class PrefixSite(FakeSite):
    """依網址前綴回應（分頁、年月參數不同時回同一份 fixture）。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for prefix, (status, body, headers) in self.routes.items():
            if url.startswith(prefix):
                return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(404)


CO = {
    "cathay": {"id": "cathay", "name": "國泰人壽", "kind": "cathay_all", "page_url": "https://cathay.test/rate",
               "api_url": "https://cathay.test/api/all"},
    "pru": {"id": "pru", "name": "保誠人壽", "kind": "pca_html", "page_url": "https://pca.test/annuity/",
            "product_name": "保誠人壽美利325美元利率變動型養老保險", "product_code": "美利325"},
    "kgi": {"id": "kgi", "name": "凱基人壽", "kind": "kgi_api", "page_url": "https://kgi.test/page",
            "list_url": "https://kgi.test/api/list", "history_url": "https://kgi.test/api/hist", "max_pages": 1},
    "taiwanlife": {"id": "taiwanlife", "name": "台灣人壽", "kind": "taiwanlife_api", "page_url": "https://tl.test/page",
                   "api_url": "https://tl.test/portal-api/Rate", "subtypes": ["B"], "max_pages": 1},
    "nanshan": {"id": "nanshan", "name": "南山人壽", "kind": "nanshan_api", "page_url": "https://ns.test/page",
                "api_url": "https://ns.test/api/List", "product_types": [14], "max_pages": 1},
}
ROUTES = {
    "https://cathay.test/api/all": (200, FX("cathay.json"), {}),
    "https://pca.test/annuity/": (200, FX("pca.html"), {}),
    "https://kgi.test/api/list": (200, FX("kgi_list.json"), {}),
    "https://kgi.test/api/hist": (200, FX("kgi_hist.json"), {}),
    "https://tl.test/portal-api/Rate": (200, FX("taiwanlife_B.json"), {}),
    "https://ns.test/api/List": (200, FX("nanshan_14.json"), {}),
}


def env(make_env, *ids, **cfg):
    site = PrefixSite(dict(ROUTES))
    db, raw, http = make_env(site)
    ad = DeclaredRatesAdapter(make_ctx(raw, http, validator_lookup(db)),
                              {"companies": [CO[i] for i in ids], "backfill_months": 12, "recent_months": 2, **cfg})
    ad.today = lambda: date(2026, 10, 5)
    return site, db, ad


def rates(db, co=None):
    sql = "SELECT * FROM declared_rates" + (" WHERE company=?" if co else "")
    return [dict(r) for r in db.conn.execute(sql, (co,) if co else ())]


def test_helpers():
    assert [parse_rate(x) for x in ("3.35%", "4.20", 3.15, "-", "--", "－", "")] == [3.35, 4.2, 3.15, None, None, None, None]
    assert slash_ym("115/10", roc=True) == "2026-10" and slash_ym("2026/9") == "2026-09"
    assert months_back(date(2026, 2, 5), 3) == [(2026, 2), (2026, 1), (2025, 12)]


def test_cathay_full_history_one_request(make_env):
    site, db, ad = env(make_env, "cathay")
    st = run_source(ad, db)
    assert st.new_items == 1 and st.errors == 0 and len(site.requests) <= 2   # robots.txt + 1 個 API
    rs = rates(db, "cathay")
    assert {r["product_code"] for r in rs} == {"CQB", "WTC"}       # 台幣商品 Z7 被過濾
    cqb = {r["month"]: r["rate_pct"] for r in rs if r["product_code"] == "CQB"}
    assert cqb["2026-10"] == 4.35 and cqb["2026-07"] == 4.35
    assert min(r["month"] for r in rs) >= "2024-11"                  # keep_months 24
    assert all(r["currency"] == "USD" and r["source_url"] == "https://cathay.test/rate" for r in rs)


def test_pca_table_and_product_guard(make_env):
    site, db, ad = env(make_env, "pru")
    run_source(ad, db)
    rs = {r["month"]: r for r in rates(db, "pru")}
    assert rs["2026-10"]["rate_pct"] == 3.0 and rs["2026-10"]["line"] == "usd_interest"
    assert len(rs) >= 12


def test_pca_missing_product_is_error_not_empty(make_env):
    """頁面上找不到設定的商品名稱 → 視為改版，丟錯不寫入。"""
    site, db, ad = env(make_env, "pru")
    ad.companies[0] = dict(CO["pru"], product_name="保誠人壽不存在的商品")
    st = run_source(ad, db)
    assert st.errors == 1 and rates(db) == []


def test_kgi_backfill_uses_history_api_then_recent_only(make_env):
    site, db, ad = env(make_env, "kgi")
    run_source(ad, db)
    hist = [r for r in site.requests if "/api/hist" in str(r.url)]
    assert hist and all(r.method == "POST" for r in hist)
    c09u = {r["month"]: r["rate_pct"] for r in rates(db, "kgi") if r["product_code"] == "C09U"}
    assert c09u["2026-10"] == 3.15 and len(c09u) >= 6
    assert all("萬能" not in r["product_name"] for r in rates(db, "kgi"))   # 名稱沒有「利率變動」的不收
    # 第二次：已有歷史 → 不再呼叫歷史 API
    n = len(hist)
    run_source(ad, db, refetch=True)
    assert len([r for r in site.requests if "/api/hist" in str(r.url)]) == n


def test_taiwanlife_json_post(make_env):
    site, db, ad = env(make_env, "taiwanlife")
    run_source(ad, db)
    post = next(r for r in site.requests if "/portal-api/Rate" in str(r.url))
    assert post.method == "POST" and b'"subtype":"B"' in post.content.replace(b" ", b"")
    rs = {r["product_code"]: r for r in rates(db, "taiwanlife")}
    assert rs["544"]["product_name"] == "台灣人壽美樂遞美元利率變動型年金保險" and rs["544"]["rate_pct"] == 3.5
    assert rs["544"]["line"] == "annuity"


def test_nanshan_header_code_and_roc_month(make_env):
    site, db, ad = env(make_env, "nanshan")
    run_source(ad, db)
    req = next(r for r in site.requests if "/api/List" in str(r.url))
    assert req.headers["Content-Type"] == "application/json" and "year=115" in str(req.url)
    rs = {r["product_code"]: r for r in rates(db, "nanshan")}
    assert rs["UTISA"]["month"] == "2026-10" and rs["UTISA"]["rate_pct"] == 3.35
    assert "【" not in rs["UTISA"]["product_name"]
    assert all("美元" in r["product_name"] for r in rs.values())


def test_one_company_failing_does_not_block_others(make_env):
    site, db, ad = env(make_env, "cathay", "pru")
    site.routes["https://cathay.test/api/all"] = (500, b"", {})
    st = run_source(ad, db)
    assert st.errors == 1 and {r["company"] for r in rates(db)} == {"pru"}


def test_rerun_same_data_is_unchanged(make_env):
    site, db, ad = env(make_env, "pru")
    run_source(ad, db)
    again = run_source(ad, db, refetch=True)
    assert again.unchanged == 1 and again.new == 0


def test_duplicate_code_prefers_current_company_name(make_env):
    site, db, ad = env(make_env, "kgi")
    run_source(ad, db)
    names = {r["product_name"] for r in rates(db, "kgi") if r["product_code"] == "C09U" and r["month"] == "2026-10"}
    assert names == {"凱基人壽美添利美元利率變動型年金保險"}
