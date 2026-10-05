"""fsc_press／fsc_penalty 最小測試：fixture 驗證筆數與欄位，並驗證「瀏覽人次」不會造成假改版。"""
from datetime import datetime

from adapters.fsc_penalty import FscPenaltyAdapter, parse_penalty_fields
from adapters.fsc_press import (FscPressAdapter, canonical_news_url, list_page_url,
                                parse_detail, parse_list)
from adapters.common import TPE
from core import events
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, fixture_bytes, make_ctx

P1 = fixture_bytes("fsc_press", "list_p1.html")
P2 = fixture_bytes("fsc_press", "list_p2.html")
DETAIL = fixture_bytes("fsc_press", "news_202609290003.html")
PENALTY_FEED = "https://www.fsc.gov.tw/RSS/Messages?serno=201202290003&language=chinese"
PRESS_CFG = {"list_pages": 2, "detail_units": ["保險局", "金融監督管理委員會", "檢查局"]}


def press_site(detail_body=DETAIL):
    routes = {list_page_url(1): (200, P1, {}), list_page_url(2): (200, P2, {})}
    for r in parse_list(P1) + parse_list(P2):
        routes[canonical_news_url(r["dataserno"])] = (200, detail_body.replace(
            b"202609290003", r["dataserno"].encode()), {})
    return FakeSite(routes)


# ---------- fsc_press ----------

def test_parse_list_fields():
    rows = parse_list(P1)
    assert len(rows) == 15
    assert rows[0] == {"dataserno": "202610020002", "date": "2026-10-02", "unit": "金融監督管理委員會",
                       "title": "本會銀行局主任秘書一職由該局陳組長家珍調任案"}
    # 列表文字被截斷時，取 title 屬性的完整標題
    assert rows[5]["title"].endswith("並獲得優惠") and "......" not in rows[5]["title"]
    assert sum(r["unit"] == "保險局" for r in rows) == 4


def test_parse_detail_fields():
    url = canonical_news_url("202609290003")
    m = parse_detail(DETAIL, url)
    assert m["detail_title"] == "壽險業115年截至7月底外幣保險商品銷售情形"
    assert m["announced_date"] == "2026-09-29"
    assert m["contact_unit"] == "保險局壽險監理組"
    assert "3,309億元" in m["body_text"] and "瀏覽人次" not in m["body_text"]
    assert len(m["attachments"]) == 2
    assert m["attachments"][0]["url"].startswith("https://www.fsc.gov.tw/uploaddowndoc?file=news/")
    assert "parse_warnings" not in m


def test_list_items_two_pages_no_detail_download(make_env):
    site = press_site()
    db, raw, http = make_env(site)
    refs = FscPressAdapter(make_ctx(raw, http), PRESS_CFG).list_items()
    assert len(refs) == 30
    assert refs[8].item_key == canonical_news_url("202609290003")
    assert refs[8].published_at == datetime(2026, 9, 29, tzinfo=TPE)
    assert site.hits("news_view") == 0


def test_scope_only_selected_units_fetch_detail(make_env):
    site = press_site()
    db, raw, http = make_env(site)
    ad = FscPressAdapter(make_ctx(raw, http, validator_lookup(db)), PRESS_CFG)
    st = run_source(ad, db)
    assert st.new_items == 30 and st.errors == 0
    in_scope = [r for r in parse_list(P1) + parse_list(P2) if r["unit"] in PRESS_CFG["detail_units"]]
    assert site.hits("news_view") == len(in_scope) == 9  # 第1頁 6 筆＋第2頁 3 筆
    types = dict(db.conn.execute("SELECT doc_type, COUNT(*) FROM raw_docs GROUP BY doc_type").fetchall())
    assert types == {"html": 9, "list_row": 21}
    ev = [e for e in events.pending(db) if e["payload"]["item_key"].endswith("202609290003&dtable=News")][0]
    assert ev["payload"]["unit"] == "保險局" and ev["payload"]["in_scope"] is True


def test_view_counter_does_not_trigger_revision(make_env, tmp_path):
    site = press_site()
    db, raw, http = make_env(site)
    ad = FscPressAdapter(make_ctx(raw, http, validator_lookup(db)), PRESS_CFG)
    run_source(ad, db)
    files_before = sorted(p.name for p in (tmp_path / "data" / "raw").rglob("*.*"))
    # 只有瀏覽人次改變
    bumped = press_site(DETAIL.replace(b"<span>1192", b"<span>1250"))
    site.routes.update(bumped.routes)
    st = run_source(ad, db, refetch=True)
    assert st.revised == 0 and st.unchanged == 30
    assert sorted(p.name for p in (tmp_path / "data" / "raw").rglob("*.*")) == files_before  # 沒留下孤兒檔


def test_real_body_change_is_revision(make_env):
    site = press_site()
    db, raw, http = make_env(site)
    ad = FscPressAdapter(make_ctx(raw, http, validator_lookup(db)), PRESS_CFG)
    run_source(ad, db)
    url = canonical_news_url("202609290003")
    site.routes[url] = (200, site.routes[url][1].replace("3,309億元".encode(), "3,310億元".encode()), {})
    st = run_source(ad, db, refetch=True)
    assert st.revised == 1
    assert len(events.pending(db, {events.DOC_REVISED})) == 1


# ---------- fsc_penalty ----------

def penalty_env(make_env):
    site = FakeSite({PENALTY_FEED: (200, fixture_bytes("fsc_penalty", "rss.xml"), {})})
    db, raw, http = make_env(site)
    ad = FscPenaltyAdapter(make_ctx(raw, http, validator_lookup(db)), {"feed_url": PENALTY_FEED})
    return site, db, raw, ad


def test_penalty_list_and_fields(make_env):
    site, db, raw, ad = penalty_env(make_env)
    refs = ad.list_items()
    assert len(refs) == 3
    assert refs[0].item_key.endswith("dataserno=202609240002&dtable=Penalty")
    assert refs[0].published_at == datetime(2026, 9, 24, tzinfo=TPE)
    doc = ad.fetch(refs[0])
    assert doc.doc_type == "rss_item"
    assert doc.meta["doc_no"] == "金管保壽字第11504937712號"
    assert doc.meta["respondent"] == "台灣人壽保險股份有限公司"
    assert doc.meta["is_insurance"] is True and doc.meta["fine_twd"] == 9_600_000
    bank = ad.fetch(refs[1]).meta
    assert bank["is_insurance"] is False and bank["fine_twd"] == 12_000_000
    assert ad.fetch(refs[2]).meta["fine_twd"] == 3_600_000  # 「（下同）360萬元」


def test_penalty_run_emits_events_with_tags(make_env):
    site, db, raw, ad = penalty_env(make_env)
    st = run_source(ad, db)
    assert st.new_items == 3 and site.hits("RSS") == 1  # 只有一個請求
    tagged = {e["payload"]["respondent"]: e["payload"]["is_insurance"] for e in events.pending(db)}
    assert tagged == {"台灣人壽保險股份有限公司": True, "合作金庫商業銀行股份有限公司": False,
                      "新光人壽保險股份有限公司": True}


def test_fine_regex_variants():
    assert parse_penalty_fields("核處新臺幣960萬元罰鍰", "")["fine_twd"] == 9_600_000
    assert parse_penalty_fields("核處新臺幣(以下同)1,400萬元罰鍰", "")["fine_twd"] == 14_000_000
    assert "fine_twd" not in parse_penalty_fields("予以糾正", "")
