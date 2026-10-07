"""fsc_draft（金管會法規草案預告）：fixture 是 2026-10-05 實際抓到的 RSS（20 筆，其中保險局 8 筆）。"""
import json
from datetime import datetime

from adapters.common import TPE
from adapters.fsc_draft import FscDraftAdapter, clean_title, parse_draft_fields, zh_number
from core import events
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, fixture_bytes, make_ctx
from tests.test_classify import run_and_classify

FEED = "https://www.fsc.gov.tw/RSS/Noticelaw?serno=201202290010&language=chinese"
CFG = {"feed_url": FEED}


def draft_env(make_env):
    site = FakeSite({FEED: (200, fixture_bytes("fsc_draft", "rss.xml"), {})})
    db, raw, http = make_env(site)
    return site, db, raw, FscDraftAdapter(make_ctx(raw, http, validator_lookup(db)), CFG)


def test_zh_number():
    assert [zh_number(s) for s in ("60", "六十", "三十", "十四", "七", "一百")] == [60, 60, 30, 14, 7, 100]
    assert zh_number("數") is None


def test_clean_title_splits_doc_no_and_period():
    t, m = clean_title("預告修正「某辦法」草案。(金管證券字第1150382705號)--預告期間：2026.9.7~2026.11.6")
    assert t == "預告修正「某辦法」草案。"
    assert m == {"comment_start": "2026-09-07", "comment_end": "2026-11-06", "title_doc_no": "金管證券字第1150382705號"}
    assert clean_title("預告「保險業辦理國外投資管理辦法」") == ("預告「保險業辦理國外投資管理辦法」", {})


def test_list_items_and_fields(make_env):
    site, db, raw, ad = draft_env(make_env)
    refs = ad.list_items()
    assert len(refs) == 20
    assert refs[0].item_key.endswith("dataserno=202609070001&dtable=NoticeLaw")
    assert "&amp;" not in refs[0].item_key
    assert refs[0].published_at == datetime(2026, 9, 7, tzinfo=TPE)
    assert "預告期間" not in refs[0].title
    metas = [ad.fetch(r).meta for r in refs]
    meta = {r.published_at.date().isoformat(): m for r, m in zip(refs, metas)}   # 只取單日只有一筆的來比對
    ins = meta["2026-08-04"]   # 保險業財務報告編製準則：國字「三十日內」
    assert ins["is_insurance"] is True and ins["doc_no"] == "金管保財字第11504924612號"
    assert ins["undertake"] == "金融監督管理委員會保險局" and ins["comment_days"] == 30
    assert ins["issued_date"] == "2026-08-04" and "comment_end" not in ins   # 不自行推算期限
    sec = meta["2026-09-07"]
    assert sec["is_insurance"] is False and sec["comment_end"] == "2026-11-06" and sec["comment_days"] == 60
    assert sum(m["is_insurance"] for m in metas) == 8
    assert site.hits("RSS") == 1   # 只有一個請求，不抓內文頁


def test_run_is_idempotent(make_env):
    site, db, raw, ad = draft_env(make_env)
    st = run_source(ad, db)
    assert st.new_items == 20
    pl = [e["payload"] for e in events.pending(db, limit=50)]
    assert sum(bool(p["is_insurance"]) for p in pl) == 8
    assert all("comment_days" in p and "undertake" in p for p in pl)
    again = run_source(ad, db)
    assert again.new == 0 and again.skipped_seen == 20


def test_missing_subject_warns_not_raises():
    m = parse_draft_fields("預告「X」", "<div>發文字號：金管法字第1號<br/>承辦單位：本會法律事務處。</div>", {"cake": "J33"})
    assert m["is_insurance"] is False and m["parse_warnings"] == ["missing subject"]


def test_classify_end_to_end(make_env):
    site = FakeSite({FEED: (200, fixture_bytes("fsc_draft", "rss.xml"), {})})
    st, rows = run_and_classify(make_env, site, FscDraftAdapter, CFG)
    assert st["ok"] == len(rows) == 20
    for r in rows.values():
        assert "法規" in json.loads(r["categories"])
    shown = [t for t, r in rows.items() if not r["hidden"]]
    assert len(shown) == 8 and all("保險" in t for t in shown)
    hidden = [r for r in rows.values() if r["hidden"]]
    assert all(r["impact"] == "low" and "非保險局草案" in json.loads(r["reasons"])[-1] for r in hidden)
