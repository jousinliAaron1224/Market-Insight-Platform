"""新聞／法規分類（M4，D22–D23）：類別、影響程度、預設隱藏，以及關鍵字陷阱。"""
import json

from adapters.fsc_penalty import FscPenaltyAdapter
from adapters.news_rss import NewsRssAdapter
from core.change_detect import run_source, validator_lookup
from core.config import load_config
from parsers.base import ParseContext
from parsers.classify import ClassifyHandler, classify
from parsers.pipeline import process_pending
from tests.conftest import FakeSite, fixture_bytes, make_ctx

PCFG = load_config()["parsing"]
RULES = PCFG["classify"]


def c(title, body="", meta=None, src="tii_law_rss"):
    return classify(title, body, meta or {}, src, RULES)


def test_focus_products_and_regulation_are_high():
    r = c("投資型保險商品銷售應注意事項", meta={"data_type": "行政規則"})
    assert r["impact"] == "high" and r["categories"] == ["商品", "法規", "通路"]
    assert c("人身保險業新契約責任準備金利率自動調整精算公式")["impact"] == "high"
    assert c("銀行、保險公司、保險代理人或保險經紀人辦理銀行保險業務應注意事項")["impact"] == "high"
    assert c("保險業自我風險及清償能力評估機制作業規範")["impact"] == "high"     # 資本制度


def test_keyword_traps():
    r = c("金管會公布 115年度上半年金融機構主要檢查缺失及改善作法", src="fsc_press", meta={"in_scope": True})
    assert "商品" not in r["categories"] and r["impact"] == "medium"         # 「上半年金融」不是年金
    r = c("保險業資金辦理專案運用公共及社會福利事業投資管理辦法第 2 條解釋令")
    assert "通路" not in r["categories"]                                      # 「辦理專案」不是理專
    r = c("中華民國人壽保險商業同業公會所屬會員辦理外幣收付非投資型人身保險業務自律規範")
    assert r["reasons"][0] == "商品: 外幣收付"                                 # 「非投資型」不算投資型
    assert r["impact"] == "high" and "外幣收付" in r["reasons"][-1]


def test_property_only_and_personnel_are_low():
    assert c("強制汽車責任保險給付標準（補登）", meta={"data_type": "法律命令"})["impact"] == "low"
    assert c("財產保險業辦理資訊公開管理辦法")["impact"] == "low"
    # 同時提到人身保險就不算「只關產險」
    assert c("財產保險業辦理資訊公開管理辦法第八條之一及人身保險業辦理資訊公開管理辦法第八條之一之解釋令")["impact"] == "medium"
    r = c("本會銀行局主任秘書一職由該局陳組長家珍調任案", src="fsc_press", meta={"in_scope": True})
    assert r["categories"] == ["人事"] and r["impact"] == "low"


def test_self_mention_and_life_penalty_press_release():
    assert c("法國巴黎人壽推出新商品", src="news_rss")["impact"] == "high"
    r = c("台灣人壽保險股份有限公司違反保險法令裁罰案", src="fsc_press", meta={"in_scope": True})
    assert r["impact"] == "high" and "法規" in r["categories"]


def test_hidden_rules():
    assert c("x 勇奪大獎", src="news_rss", meta={"pr_noise": True})["hidden"] is True
    r = c("金管會開放銀行申請試辦存款代幣業務", src="fsc_press", meta={"in_scope": False, "unit": "銀行局"})
    assert r["hidden"] is True and r["impact"] == "low"


def run_and_classify(make_env, site, adapter_cls, cfg):
    db, raw, http = make_env(site)
    ad = adapter_cls(make_ctx(raw, http, validator_lookup(db)), cfg)
    run_source(ad, db)
    ctx = ParseContext(db=db, raw=raw, config=PCFG)
    st = process_pending(ctx, [ClassifyHandler()])
    rows = {r["title"]: dict(r) for r in db.conn.execute("SELECT * FROM doc_labels")}
    return st, rows


def test_news_end_to_end_uses_transient_lead(make_env):
    ctee, cna = "https://ctee/rss", "https://cna/rss"
    site = FakeSite({ctee: (200, fixture_bytes("news_rss", "ctee_insurance.xml"), {}),
                     cna: (200, fixture_bytes("news_rss", "cna_finance.xml"), {})})
    cfg = {"feeds": [{"name": "ctee_insurance", "url": ctee, "keyword_filter": False},
                     {"name": "cna_finance", "url": cna, "keyword_filter": True}]}
    st, rows = run_and_classify(make_env, site, NewsRssAdapter, cfg)
    assert st["ok"] == 16 and len(rows) == 16
    pr = rows["保誠人壽勇奪2026卓越保險永續行動與品牌創新兩大獎項"]
    assert pr["hidden"] == 1 and pr["impact"] == "low"
    hr = rows["金管會新人事案　陳家珍任銀行局主秘"]
    assert "人事" in json.loads(hr["categories"])
    assert sum(r["hidden"] for r in rows.values()) >= 5


def test_penalty_end_to_end(make_env):
    feed = "https://www.fsc.gov.tw/RSS/Messages?serno=201202290003&language=chinese"
    site = FakeSite({feed: (200, fixture_bytes("fsc_penalty", "rss.xml"), {})})
    st, rows = run_and_classify(make_env, site, FscPenaltyAdapter, {"feed_url": feed})
    assert st["ok"] == len(rows) > 0
    for r in rows.values():
        reasons = json.loads(r["reasons"])
        assert "法規" in json.loads(r["categories"])
        if r["hidden"]:
            assert r["impact"] == "low" and "非保險業裁罰" in reasons[-1]
    assert any(r["impact"] == "high" for r in rows.values())                # 壽險同業被罰
