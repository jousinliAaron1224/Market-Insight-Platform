"""tii_law_rss 最小測試：用存好的 feed / 內文頁 fixture 驗證筆數與欄位（Handbook 規則）。"""
from datetime import datetime

import pytest

from adapters.base import NotModified
from adapters.tii_law_rss import TiiLawRssAdapter, parse_detail, parse_roc_date, TPE
from tests.conftest import FakeSite, fixture_bytes, make_ctx

FEED = "https://law.tii.org.tw/Fn/rss.asp"
DETAIL = "https://law.tii.org.tw/Fn/ShowNews.asp?id=3867"
CFG = {"tier": "A", "schedule": "0 */2 * * *", "feed_url": FEED,
       "fetch_detail": True, "respect_robots": False}


def site(**extra):
    routes = {
        FEED: (200, fixture_bytes("tii_law_rss", "rss.xml"), {"Content-Type": "text/xml"}),
        DETAIL: (200, fixture_bytes("tii_law_rss", "shownews_3867.html"), {"Content-Type": "text/html"}),
    }
    routes.update(extra)
    return FakeSite(routes)


def test_parse_roc_date():
    assert parse_roc_date("1150930") == datetime(2026, 9, 30, tzinfo=TPE)
    assert parse_roc_date("115.07.14") == datetime(2026, 7, 14, tzinfo=TPE)
    assert parse_roc_date("") is None and parse_roc_date("abc") is None
    assert parse_roc_date("1151399") is None


def test_list_items_count_and_fields(make_env):
    s = site()
    db, raw, http = make_env(s)
    refs = TiiLawRssAdapter(make_ctx(raw, http), CFG).list_items()
    assert len(refs) == 50
    first = refs[0]
    assert first.source_id == "tii_law_rss"
    assert first.item_key == "保險事業發展中心/3881"
    assert first.url == "https://law.tii.org.tw/Fn/ShowNews.asp?id=3881"
    assert first.title == "保險代理人管理規則（補登）"
    assert first.published_at == datetime(2026, 9, 30, tzinfo=TPE)
    # 必要欄位全有值、item_key 不重複
    assert all(r.title and r.url and r.published_at for r in refs)
    assert len({r.item_key for r in refs}) == 50
    # list_items 絕不下載內文
    assert s.hits("ShowNews") == 0


def test_fetch_stores_item_and_detail(make_env, tmp_path):
    s = site()
    db, raw, http = make_env(s)
    ad = TiiLawRssAdapter(make_ctx(raw, http), CFG)
    ref = next(r for r in ad.list_items() if r.item_key.endswith("/3867"))
    doc = ad.fetch(ref)
    assert doc.doc_type == "html"
    assert doc.raw_path.startswith("raw/tii_law_rss/") and doc.raw_path.endswith(f"{doc.content_hash}.html")
    assert raw.read(doc.raw_path) == fixture_bytes("tii_law_rss", "shownews_3867.html")  # 原封不動
    assert raw.read(doc.meta["rss_item_path"]).decode().count("3867") >= 1
    assert doc.meta["data_type"] == "行政規則"
    assert doc.meta["detail_title"] == "人身保險業辦理利率變動型保險商品業務應注意事項"
    assert doc.meta["announced_date"] == "115.07.14"
    assert doc.meta["attachments"][0]["url"] == "https://law.tii.org.tw/Fn/download.asp?fid=3721"
    assert "parse_warnings" not in doc.meta


def test_fetch_is_idempotent_on_disk(make_env, tmp_path):
    s = site()
    db, raw, http = make_env(s)
    ad = TiiLawRssAdapter(make_ctx(raw, http), CFG)
    ref = next(r for r in ad.list_items() if r.item_key.endswith("/3867"))
    a, b = ad.fetch(ref), ad.fetch(ref)
    assert (a.raw_path, a.content_hash) == (b.raw_path, b.content_hash)
    assert len(list((tmp_path / "data" / "raw").rglob("*.*"))) == 2  # 1 html + 1 json


def test_fetch_raises_not_modified_on_304(make_env):
    s = site(**{DETAIL: (200, fixture_bytes("tii_law_rss", "shownews_3867.html"), {"ETag": '"v1"'})})
    db, raw, http = make_env(s)
    ad = TiiLawRssAdapter(make_ctx(raw, http, validators=lambda src, k: ('"v1"', None)), CFG)
    ref = next(r for r in ad.list_items() if r.item_key.endswith("/3867"))
    with pytest.raises(NotModified):
        ad.fetch(ref)


def test_rss_only_mode(make_env):
    s = site()
    db, raw, http = make_env(s)
    ad = TiiLawRssAdapter(make_ctx(raw, http), {**CFG, "fetch_detail": False})
    doc = ad.fetch(ad.list_items()[0])
    assert doc.doc_type == "rss_item" and doc.raw_path.endswith(".json")
    assert s.hits("ShowNews") == 0


def test_parse_detail_flags_layout_change():
    meta = parse_detail(b"<html><body>site redesigned</body></html>", DETAIL)
    assert set(meta["parse_warnings"]) == {"missing DataType", "missing title row", "missing body <pre>"}
