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
    db.conn.commit()
    st = process_pending(ParseContext(db=db, raw=raw, config=load_config()["parsing"]))
    assert st["ok"] == 3
    db.close()
    store = Store(data / "intel.db", data)
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
