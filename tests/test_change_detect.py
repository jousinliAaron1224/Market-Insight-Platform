"""三層變動偵測與 raw 儲存的端到端測試（模擬網站，不打真實網路）。"""
import json

from adapters.tii_law_rss import TiiLawRssAdapter
from core import events
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, fixture_bytes, make_ctx

FEED = "https://law.tii.org.tw/Fn/rss.asp"
CFG = {"feed_url": FEED, "fetch_detail": True, "respect_robots": False}
DETAIL_HTML = fixture_bytes("tii_law_rss", "shownews_3867.html")
FEED_XML = fixture_bytes("tii_law_rss", "rss.xml")
IDS = [l.split(b"id=")[1].split(b"<")[0].decode() for l in FEED_XML.split(b"\n") if b"<link>" in l and b"id=" in l]


def make_site(detail_headers=None, body_for=None):
    routes = {FEED: (200, FEED_XML, {})}
    for i in IDS:
        body = (body_for or {}).get(i, DETAIL_HTML.replace(b"3721", i.encode()))
        routes[f"https://law.tii.org.tw/Fn/ShowNews.asp?id={i}"] = (200, body, detail_headers or {})
    return FakeSite(routes)


def setup(make_env, site):
    db, raw, http = make_env(site)
    ad = TiiLawRssAdapter(make_ctx(raw, http, validator_lookup(db)), CFG)
    return db, raw, ad


def count(db, table, where="1=1"):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def test_first_run_emits_new_item_per_listing(make_env, tmp_path):
    site = make_site()
    db, raw, ad = setup(make_env, site)
    st = run_source(ad, db)
    assert (st.listed, st.new_items, st.errors) == (50, 50, 0)
    assert count(db, "events", "type='new_item'") == 50
    assert count(db, "raw_docs") == 50 and count(db, "seen_items") == 50
    ev = events.pending(db)[0]
    assert ev["payload"]["version"] == 1 and ev["raw_doc_id"]
    # raw 檔都在、路徑格式正確
    for row in db.conn.execute("SELECT raw_path, content_hash FROM raw_docs"):
        assert raw.read(row["raw_path"])
        assert row["raw_path"].endswith(row["content_hash"] + ".html")
    run = db.conn.execute("SELECT * FROM crawl_runs").fetchone()
    assert (run["status"], run["items_listed"], run["items_new"]) == ("ok", 50, 50)


def test_layer1_second_run_downloads_nothing(make_env):
    site = make_site()
    db, raw, ad = setup(make_env, site)
    run_source(ad, db)
    before = site.hits("ShowNews")
    st = run_source(ad, db)
    assert st.skipped_seen == 50 and st.new == 0
    assert site.hits("ShowNews") == before  # 列表層擋下，未下載任何內文
    assert count(db, "events") == 50


def test_layer2_304_skips_download(make_env):
    site = make_site(detail_headers={"ETag": '"v1"'})
    db, raw, ad = setup(make_env, site)
    run_source(ad, db)
    st = run_source(ad, db, refetch=True)
    assert st.not_modified == 50 and st.new == 0
    sent = [r for r in site.requests if "ShowNews" in str(r.url)][-1]
    assert sent.headers["If-None-Match"] == '"v1"'


def test_layer3_same_hash_discarded(make_env, tmp_path):
    site = make_site()
    db, raw, ad = setup(make_env, site)
    run_source(ad, db)
    files_before = len(list((tmp_path / "data" / "raw").rglob("*.*")))
    st = run_source(ad, db, refetch=True)
    assert st.unchanged == 50 and st.new == 0
    assert count(db, "raw_docs") == 50 and count(db, "events") == 50
    assert len(list((tmp_path / "data" / "raw").rglob("*.*"))) == files_before


def test_content_change_creates_version_and_doc_revised(make_env):
    site = make_site()
    db, raw, ad = setup(make_env, site)
    run_source(ad, db)
    url = "https://law.tii.org.tw/Fn/ShowNews.asp?id=3867"
    original = site.routes[url][1]
    site.routes[url] = (200, original.replace("（以下節錄省略）".encode("cp950"), "（更正）".encode("cp950")), {})
    st = run_source(ad, db, refetch=True)
    assert st.revised == 1 and st.unchanged == 49
    rows = db.conn.execute(
        "SELECT version, raw_path FROM raw_docs WHERE item_key='保險事業發展中心/3867' ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    assert raw.read(rows[0]["raw_path"]) == original  # 舊版保留，未被覆寫
    ev = [e for e in events.pending(db, {events.DOC_REVISED})]
    assert len(ev) == 1 and ev[0]["payload"]["previous_version"] == 1


def test_list_fingerprint_change_triggers_recheck(make_env):
    site = make_site()
    db, raw, ad = setup(make_env, site)
    run_source(ad, db)
    feed2 = FEED_XML.replace("保險代理人管理規則（補登）".encode(), "保險代理人管理規則（更正）".encode())
    site.routes[FEED] = (200, feed2, {})
    before = site.hits("ShowNews")
    st = run_source(ad, db)  # 不加 refetch
    assert st.skipped_seen == 49
    assert site.hits("ShowNews") == before + 1  # 只有標題改了的那筆被重抓
    assert st.unchanged == 1  # 內文沒變 → 第 3 層丟棄


def test_single_item_failure_is_retried_next_run(make_env):
    site = make_site()
    site.routes["https://law.tii.org.tw/Fn/ShowNews.asp?id=3881"] = (500, b"", {})
    db, raw, ad = setup(make_env, site)
    st = run_source(ad, db)
    assert st.errors == 1 and st.new_items == 49
    run = db.conn.execute("SELECT status, errors FROM crawl_runs").fetchone()
    assert tuple(run) == ("partial", 1)
    site.routes["https://law.tii.org.tw/Fn/ShowNews.asp?id=3881"] = (200, DETAIL_HTML, {})
    st = run_source(ad, db)
    assert st.new_items == 1 and st.skipped_seen == 49


def test_list_failure_marks_run_error(make_env):
    site = make_site()
    site.routes[FEED] = (503, b"", {})
    db, raw, ad = setup(make_env, site)
    st = run_source(ad, db)
    run = db.conn.execute("SELECT status, items_listed, error_detail FROM crawl_runs").fetchone()
    assert run["status"] == "error" and run["items_listed"] == 0 and "list_items" in run["error_detail"]
    assert st.listed == 0
