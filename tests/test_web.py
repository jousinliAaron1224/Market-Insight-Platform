"""前端原型伺服器（web/server.py）：API 查詢、靜態頁、原文檔案與路徑防護。"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from core.config import load_config
from core.storage import Database, RawStore
from parsers.base import ParseContext
from parsers.pipeline import process_pending
from tests.test_demo_replay import add
from tests.conftest import fixture_bytes
from web.server import Store, make_handler

KGI = fixture_bytes("companies", "kgi", "clause_sample.pdf")
TII = fixture_bytes("tii_law_rss", "shownews_3867.html")
NAME = "凱基人壽鑫旺九九外幣變額年金保險(112)"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    data = tmp_path_factory.mktemp("web") / "data"
    db = Database(data / "intel.db")
    db.init_schema()
    raw = RawStore(data / "raw")
    add(db, raw, "tii_law_rss", "t1", "html", TII, {"title": "投資型保險商品銷售應注意事項",
        "published_at": "2026-09-20T00:00:00+08:00", "data_type": "行政規則"}, "html")
    add(db, raw, "tii_law_rss", "t2", "html", TII + b" ", {"title": "強制汽車責任保險給付標準",
        "published_at": "2026-09-21T00:00:00+08:00", "data_type": "法律命令"}, "html")
    add(db, raw, "company_kgi_products", f"k:{NAME}", "pdf", KGI,
        {"company": "凱基人壽", "line": "變額年金保險", "currency": "USD", "title": NAME,
         "first_date": "2023-08-14", "latest_date": "2025-01-01", "clause_url": "https://x/c.pdf"}, "pdf")
    db.conn.execute("INSERT INTO products (company, name, line, currency, status) VALUES (?,?,?,?, 'on_sale')",
                    ("凱基人壽", NAME, "變額年金保險", "USD"))
    for name, f in (("ib_premium_monthly", "tii_i10_trimmed.csv"), ("lia_performance", "lia_14539.csv"),
                    ("life_indicators", "ib_7191_synthetic.json")):
        add(db, raw, "open_data", f"{name}:0", f.rsplit(".", 1)[1], fixture_bytes("open_data", f),
            {"title": name, "dataset": name, "final_url": "https://example.org/x.csv",
             "event_extra": {"dataset": name}}, "csv")
    db.conn.commit()
    st = process_pending(ParseContext(db=db, raw=raw, config=load_config()["parsing"]))
    assert st["ok"] == 3
    db.close()
    store = Store(data / "intel.db", data, load_config()["parsing"]["product_impact"])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}", data
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_meta_and_labels(site):
    store, base, _ = site
    _, _, body = get(base, "/api/meta")
    m = json.loads(body)
    assert m["parsed"] and m["impact_counts"] == {"high": 1, "low": 1} and m["companies"] == ["凱基人壽"]
    _, _, body = get(base, "/api/labels?impact=high,medium")
    data = json.loads(body)
    assert data["total"] == 1 and data["items"][0]["title"] == "投資型保險商品銷售應注意事項"
    assert data["items"][0]["raw"].startswith("/raw/tii_law_rss/")
    assert json.loads(get(base, "/api/labels?impact=low&q=" + urllib.parse.quote("汽車"))[2])["total"] == 1
    assert json.loads(get(base, "/api/labels?category=" + urllib.parse.quote("人事"))[2])["total"] == 0


def test_product_and_articles(site):
    store, base, _ = site
    items = json.loads(get(base, "/api/products?has_clause=1")[2])["items"]
    assert [i["name"] for i in items] == [NAME] and items[0]["articles"] == 36
    p = json.loads(get(base, "/api/product?" + urllib.parse.urlencode({"company": "凱基人壽", "name": NAME}))[2])
    assert p["clause"]["fields"]["currency"] == "USD" and p["clause"]["evidence"]["currency"]["page"]
    assert p["first_date"] == "2023-08-14" and p["versions"][0]["version"] == 1
    arts = json.loads(get(base, f"/api/articles?raw_doc_id={p['clause']['raw_doc_id']}")[2])
    assert arts[0]["kind"] == "preamble" and arts[1]["title"] == "保險契約的構成"
    status, ctype, body = get(base, p["clause"]["raw"])
    assert ctype == "application/pdf" and body.startswith(b"%PDF")


def test_static_pages(site):
    _, base, _ = site
    for path in ("/", "/compare.html", "/common.js", "/style.css"):
        status, ctype, _ = get(base, path)
        assert status == 200 and "charset=utf-8" in ctype


@pytest.mark.parametrize("path", ["/raw/../intel.db", "/raw/%2e%2e/intel.db", "/raw/%2e%2e%2fintel.db",
                                  "/../intel.db", "/api/product?company=x&name=y", "/nope.html"])
def test_not_found_and_traversal(site, path):
    _, base, _ = site
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base, path)
    assert e.value.code == 404


def test_tii_html_served_as_big5(site):
    store, base, _ = site
    raw = json.loads(get(base, "/api/labels?hidden=all")[2])["items"][0]["raw"]
    _, ctype, _ = get(base, raw)
    assert ctype == "text/html; charset=big5"


def test_week_groups_and_product_impact(site):
    store, base, _ = site
    w = json.loads(get(base, "/api/week?days=7")[2])
    assert w["as_of"] == "2026-09-21" and w["from"] == "2026-09-15"
    assert w["counts"] == {"law": {"total": 2, "high": 1}, "news": {"total": 0, "high": 0}}
    assert [i["title"] for i in w["focus"]] == ["投資型保險商品銷售應注意事項"]     # 高影響；產險那則不算
    law = json.loads(get(base, "/api/labels?group=law&hidden=all")[2])
    assert law["total"] == 2 and json.loads(get(base, "/api/labels?group=news")[2])["total"] == 0
    item = next(i for i in law["items"] if i["impact"] == "high")
    assert item["group"] == "law" and item["product_impact"]["scope"] == "全部投資型商品"
    assert item["product_impact"]["competitor_count"] == 1            # 測試資料只有一個凱基商品（競品）
    d = json.loads(get(base, f"/api/impact?raw_doc_id={item['raw_doc_id']}")[2])
    assert d["product_impact"]["competitors"] == {"凱基人壽": 1} and d["product_impact"]["self_products"] == []


def test_market_supply(site):
    _, base, _ = site
    s = json.loads(get(base, "/api/market/supply")[2])
    assert s["as_of"] == "2025-01-01" and [c["company"] for c in s["companies"]] == ["凱基人壽"]
    c = s["companies"][0]
    assert (c["on_sale"], c["families"], c["foreign"]) == (1, 1, 1)
    assert s["revision_waves"][0]["month"] == "2025-01" and s["conclusions"]
    assert get(base, "/market.html")[0] == 200
    status, ctype, body = get(base, "/charts.js")
    assert status == 200 and "javascript" in ctype and b"function columns" in body


def test_market_demand(site):
    _, base, _ = site
    d = json.loads(get(base, "/api/market/demand")[2])
    assert d["as_of"] == "2026-06" and not d["errors"]
    tot = next(r for r in d["ytd"] if r["line"] == "合計")
    assert tot["this"] == 16310.9 and tot["growth"] == 23.7
    assert "個人年金" in d["conclusions"][1] and "+103.6%" in d["conclusions"][1]
    assert d["investment_ref"]["summary"].startswith("初年度保費投資型 2,760 億元")
    assert get(base, d["source"]["raw"])[0] == 200


def test_market_companies(site):
    _, base, _ = site
    c = json.loads(get(base, "/api/market/companies")[2])
    assert c["period"] == "2026Q2" and c["n_companies"] == 8 and not c["errors"]
    assert c["matched"]["凱基人壽"] == "凱基人壽保險股份有限公司"            # 測試資料庫只有凱基的商品＋自家
    ind = {i["name"]: i for i in c["indicators"]}
    assert ind["保費收入變動率"]["values"]["凱基人壽"] == {"value": 10.0, "rank": 4}
    assert ind["新契約費用率"]["n"] == 7                                   # N/A 不算
    assert len(c["growth_trend"]) == 2


def test_family_names():
    from web.market import family_of
    assert family_of("法商法國巴黎人壽", "法商法國巴黎人壽享富足外幣變額年金保險(乙型)") == "享富足"
    assert family_of("凱基人壽", "凱基人壽鑫旺九九外幣變額年金保險(112)") == "鑫旺九九"
