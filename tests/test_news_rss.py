"""news_rss 最小測試：兩個 feed 的筆數與欄位、關鍵字過濾、公關稿標記、導言只進暫存表。"""
from datetime import datetime, timedelta, timezone

from adapters.common import TPE
from adapters.news_rss import NewsRssAdapter, parse_pubdate
from core import events
from core.change_detect import run_source, validator_lookup
from core.storage import iso
from tests.conftest import FakeSite, fixture_bytes, make_ctx

CTEE = "https://www.ctee.com.tw/rss_web/category/insurance"
CNA = "https://feeds.feedburner.com/rsscna/finance"
CFG = {"feeds": [{"name": "ctee_insurance", "url": CTEE, "keyword_filter": False},
                 {"name": "cna_finance", "url": CNA, "keyword_filter": True}]}


def env(make_env, **routes):
    site = FakeSite({CTEE: (200, fixture_bytes("news_rss", "ctee_insurance.xml"), {}),
                     CNA: (200, fixture_bytes("news_rss", "cna_finance.xml"), {}), **routes})
    db, raw, http = make_env(site)
    return site, db, raw, NewsRssAdapter(make_ctx(raw, http, validator_lookup(db)), CFG)


def test_parse_pubdate():
    assert parse_pubdate("2026-10-02T03:00:00") == datetime(2026, 10, 2, 3, 0, tzinfo=TPE)
    assert parse_pubdate("Fri, 02 Oct 2026 18:51:15 +0800").astimezone(timezone.utc) == \
        datetime(2026, 10, 2, 10, 51, 15, tzinfo=timezone.utc)
    assert parse_pubdate("") is None


def test_list_counts_and_keyword_filter(make_env):
    site, db, raw, ad = env(make_env)
    refs = ad.list_items()
    ctee = [r for r in refs if r.item_key.startswith("ctee_insurance:")]
    cna = [r for r in refs if r.item_key.startswith("cna_finance:")]
    assert len(ctee) == 15                      # 保險專版全收
    assert [r.title for r in cna] == ["金管會新人事案　陳家珍任銀行局主秘"]  # 20 筆只留 1 筆
    assert ctee[0].item_key == "ctee_insurance:20261002700158"
    assert ctee[0].url == "https://www.ctee.com.tw/news/20261002700158-430305"
    assert all(r.published_at for r in refs)


def test_pr_noise_rules_on_real_titles(make_env):
    site, db, raw, ad = env(make_env)
    ad.list_items()
    noise = {e["title"] for e in ad._entries.values() if e["pr_hits"]}
    assert noise == {
        "磊山保經AI智慧營運管理平台 獲《哈佛商業評論》數位轉型鼎革獎",
        "保誠人壽勇奪2026卓越保險永續行動與品牌創新兩大獎項",
        "新安東京海上產險奪卓越保險三大獎 品牌形象連三年獲肯定",
        "元大人壽獲卓越保險評比專業肯定 洞察超高齡社會需求 打造彈性退休保障",
        "法國巴黎人壽勇奪2026卓越保險評比三項大獎 堅持商品創新、社會參與與人才培育 創造共好價值",
        "元大人壽推出「全台好騙狗狗大賞」防詐活動 提升防詐意識同時為導盲犬公益集氣",
        "連比爾蓋茲都愛！ 首屆新光認真盃匹克球公開賽登場 新光人壽邀請全民揪伴組隊熱血揮拍",
    }
    # 實質新聞不可誤判（「獲利」不是「獲獎」）
    assert "壽險前八月獲利 大三元" not in noise
    assert "聯準會升息 壽險公司10月美元宣告利率「以靜制動」" not in noise


def test_run_lead_goes_to_transient_table_only(make_env, tmp_path):
    site, db, raw, ad = env(make_env)
    st = run_source(ad, db)
    assert st.new_items == 16 and st.errors == 0
    assert site.hits("ctee.com.tw/news") == 0 and site.hits("cna.com.tw/news") == 0  # 不抓內文頁
    # 導言不在 raw 檔、也不在 raw_docs.meta
    for row in db.conn.execute("SELECT raw_path, meta FROM raw_docs"):
        assert "國泰金控1日代子公司" not in raw.read(row["raw_path"]).decode()
        assert "transient_lead" not in row["meta"] and "國泰金控1日" not in row["meta"]
    lead = db.get_lead("news_rss", "ctee_insurance:20261002700158")
    assert lead.startswith("國泰金控1日代子公司國泰人壽發布三則重大訊息")
    ev = {e["payload"]["title"]: e["payload"] for e in events.pending(db, limit=100)}
    assert ev["保誠人壽勇奪2026卓越保險永續行動與品牌創新兩大獎項"]["pr_noise"] is True
    assert ev["壽險前八月獲利 大三元"]["pr_noise"] is False
    assert ev["金管會新人事案　陳家珍任銀行局主秘"]["feed"] == "cna_finance"


def test_expired_leads_are_purged(make_env):
    site, db, raw, ad = env(make_env)
    run_source(ad, db)
    past = iso(datetime.now(timezone.utc) - timedelta(days=1))
    db.conn.execute("UPDATE news_leads SET expires_at=?", (past,))
    db.conn.commit()
    run_source(ad, db)  # 每輪開始時清除過期導言
    assert db.conn.execute("SELECT COUNT(*) FROM news_leads").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM raw_docs").fetchone()[0] == 16  # 永久資料不受影響


def test_one_feed_down_is_partial_not_fatal(make_env):
    site, db, raw, ad = env(make_env, **{CNA: (503, b"", {})})
    st = run_source(ad, db)
    assert st.new_items == 15 and st.errors == 1
    run = db.conn.execute("SELECT status, error_detail FROM crawl_runs").fetchone()
    assert run["status"] == "partial" and "cna_finance" in run["error_detail"]


def test_filtered_to_zero_is_not_an_error(make_env):
    """ctee 被擋、cna 成功但 0 筆符合關鍵字：不算整輪失敗（2026-10-03 實際情況）。"""
    empty_cna = fixture_bytes("news_rss", "cna_finance.xml").replace("金管會新人事案".encode(), "某某新人事案".encode())
    empty_cna = empty_cna.replace("（中央社記者蘇思云台北2日電）金管會".encode(), "（中央社記者蘇思云台北2日電）某會".encode())
    site, db, raw, ad = env(make_env, **{CTEE: (403, b"", {}), CNA: (200, empty_cna, {})})
    st = run_source(ad, db)
    run = db.conn.execute("SELECT status FROM crawl_runs").fetchone()
    assert st.listed == 0 and st.errors == 1 and run["status"] == "partial"


def test_disabled_feed_is_not_requested(make_env):
    site = FakeSite({CNA: (200, fixture_bytes("news_rss", "cna_finance.xml"), {})})
    db, raw, http = make_env(site)
    cfg = {"feeds": [{**CFG["feeds"][0], "enabled": False}, CFG["feeds"][1]]}
    ad = NewsRssAdapter(make_ctx(raw, http, validator_lookup(db)), cfg)
    st = run_source(ad, db)
    assert st.errors == 0 and st.new_items == 1
    assert site.hits("ctee") == 0


def test_ltn_business_feed_configured_and_filtered(make_env):
    """D26：自由時報財經 RSS 已設定、啟用並以關鍵字過濾（fixture 為合成資料，只驗證格式處理）。"""
    from core.config import load_config, source_config
    feeds = {f["name"]: f for f in source_config(load_config(), "news_rss")["feeds"]}
    ltn = feeds["ltn_business"]
    assert ltn.get("enabled", True) and ltn["keyword_filter"] is True
    site = FakeSite({ltn["url"]: (200, fixture_bytes("news_rss", "ltn_business_synthetic.xml"), {})})
    db, raw, http = make_env(site)
    ad = NewsRssAdapter(make_ctx(raw, http, validator_lookup(db)), {"feeds": [ltn]})
    st = run_source(ad, db)
    assert (st.listed, st.new_items, st.errors) == (1, 1, 0)
    ev = events.pending(db)[0]["payload"]
    assert ev["feed"] == "ltn_business" and ev["item_key"].startswith("ltn_business:")
    assert db.get_lead("news_rss", ev["item_key"]).startswith("測試導言")
