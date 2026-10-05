"""條款 PDF 結構化（M4，D22）：三種版面（法巴雙欄、凱基【】標題、國泰同行標題）與事件 handler。"""
import json

import pytest

from core import events
from core.storage import Database, RawStore, utcnow
from parsers.base import Enricher, ParseContext
from parsers.clause import ClauseHandler
from parsers.clause_pdf import article_diff, cn_number, parse_clause_pdf, parse_filings
from parsers.pipeline import process_pending
from tests.conftest import fixture_bytes

CARDIF = fixture_bytes("companies", "cardif", "clause_UA0020.pdf")
KGI = fixture_bytes("companies", "kgi", "clause_sample.pdf")
CATHAY = fixture_bytes("companies", "cathay", "clause_sample.pdf")


@pytest.fixture(scope="module")
def parsed():
    return {"cardif": parse_clause_pdf(CARDIF), "kgi": parse_clause_pdf(KGI), "cathay": parse_clause_pdf(CATHAY)}


def test_cn_number():
    assert [cn_number(x) for x in ("一", "十", "十四", "二十一", "四十三", "一百零二", "12")] == [1, 10, 14, 21, 43, 102, 12]


def test_articles_are_sequential_and_titled(parsed):
    for key, n_articles in (("cardif", 37), ("kgi", 36), ("cathay", 42)):
        r = parsed[key]
        arts = r.articles
        assert len(arts) == n_articles, key
        assert [a.article_no for a in arts] == list(range(1, n_articles + 1)), key
        assert all(a.title for a in arts), key
        assert arts[0].title == "保險契約的構成" and arts[-1].title == "管轄法院"
        assert not r.warnings


def test_two_column_reading_order(parsed):
    """法巴雙欄：左欄讀完才讀右欄，條文不會左右交錯；首頁右欄的文號不混進條文。"""
    r = parsed["cardif"]
    assert r.two_column
    defs = next(a for a in r.articles if a.title == "名詞定義")
    assert "預定利率:係指本公司於年金給付開始日用以計算年金金額之利率,本公司將參考" in defs.flat
    assert not any("文號" in a.text for a in r.articles)
    assert r.articles[4].title == "保險範圍" and r.articles[4].as_row(0)["page_start"] == 2


def test_headers_and_footers_removed(parsed):
    for r in parsed.values():
        text = "\n".join(s.text for s in r.sections)
        assert "第1頁" not in text.replace(" ", "")
    assert "BNAGSVA" not in "\n".join(s.text for s in parsed["kgi"].sections)


def test_fields_with_evidence(parsed):
    c = parsed["cathay"].fields
    assert c["coverage"] == ["祝壽保險金", "身故保險金", "喪葬費用保險金", "完全失能保險金"]
    assert c["exclusions"].startswith("有下列情形之一者,本公司不負給付保險金的責任")
    assert parsed["cathay"].evidence["exclusions"] == {"article_no": 30, "title": "除外責任", "page": 9}
    assert c["free_look_days"] == 10 and c["participating"] is False

    k = parsed["kgi"]
    assert k.fields["currency"] == "USD" and k.evidence["currency"]["title"] == "貨幣單位與匯率計算"
    assert set(k.fields["rate_terms"]) == {"宣告利率", "預定利率"}
    assert k.fields["accumulation_min_years"] == 6
    assert "issue_age" in k.missing           # 投保年齡不在條款裡，留給 LLM／要保規則

    f = parsed["cardif"].fields
    assert f["coverage"] == ["返還保單帳戶價值", "年金", "未支領之年金餘額"]
    assert f["accumulation_min_years"] == 10
    assert f["filings"][0] == {"date": "2014-10-01", "doc_no": "巴黎(103)壽字第10005號", "kind": "備查"}
    assert f["latest_filing_date"] == "2025-01-01"


def test_parse_filings_variants():
    got = parse_filings("114.07.01富壽商精字第1140001682號函備查\n"
                        "逕修文號:民國112年02月08日依111年08月30日金管保\n壽字第1110445485號函修正")
    assert got == [{"date": "2025-07-01", "doc_no": "富壽商精字第1140001682號", "kind": "備查"},
                   {"date": "2023-02-08", "doc_no": "金管保壽字第1110445485號", "kind": "逕修"}]


def test_article_diff():
    old = [{"kind": "article", "article_no": 1, "title": "a", "text_hash": "x"},
           {"kind": "article", "article_no": 2, "title": "b", "text_hash": "y"}]
    new = [{"kind": "article", "article_no": 1, "title": "a", "text_hash": "x"},
           {"kind": "article", "article_no": 2, "title": "b", "text_hash": "z"},
           {"kind": "article", "article_no": 3, "title": "c", "text_hash": "w"}]
    assert article_diff(old, new) == {"added": [3], "removed": [], "changed": [{"article_no": 2, "title": "b"}]}


# ---------------- handler（事件 → 資料表） ----------------
def setup_db(tmp_path):
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    raw = RawStore(tmp_path / "raw")
    return db, raw


def add_version(db, raw, pdf, version, prev=None, name="凱基人壽鑫旺九九外幣變額年金保險(112)", extra=None):
    path, digest = raw.put("company_kgi_products", pdf, "pdf", utcnow())
    meta = {"title": name, "company": "凱基人壽", "line": "變額年金保險", "currency": "FX",
            "clause_url": "https://example/clause.pdf"}
    rid = db.conn.execute(
        """INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
           VALUES ('company_kgi_products', ?, ?, ?, ?, '2026-10-03T00:00:00+00:00', 'u', 'pdf', ?)""",
        (f"company_kgi_products:{name}", version, digest, path, json.dumps(meta, ensure_ascii=False))).lastrowid
    pl = {"source_id": "company_kgi_products", "item_key": f"company_kgi_products:{name}", "title": name,
          "doc_type": "pdf", "version": version, "company": "凱基人壽"}
    if prev:
        pl.update(previous_raw_doc_id=prev, previous_version=version - 1)
    pl.update(extra or {})
    events.emit(db, events.DOC_REVISED if prev else events.NEW_ITEM, rid, pl)
    db.conn.commit()
    return rid


def test_handler_new_item_and_revision(tmp_path):
    db, raw = setup_db(tmp_path)
    db.conn.execute("INSERT INTO products (company, name, status) VALUES ('凱基人壽', '凱基人壽鑫旺九九外幣變額年金保險(112)', 'on_sale')")
    ctx = ParseContext(db=db, raw=raw)
    v1 = add_version(db, raw, KGI, 1)
    seen = []
    st = process_pending(ctx, [ClauseHandler()], on_event=lambda e, s, sm: seen.append(sm))
    assert st["ok"] == 1 and seen[0]["articles"] == 36
    terms = db.conn.execute("SELECT * FROM product_terms").fetchone()
    assert (terms["currency"], terms["status"], terms["clause_version"]) == ("USD", "on_sale", 1)
    assert json.loads(terms["coverage"]) == ["返還保單帳戶價值", "年金給付"]
    assert db.conn.execute("SELECT COUNT(*) FROM clause_articles WHERE raw_doc_id=?", (v1,)).fetchone()[0] == 38   # 前言＋36 條＋附表

    v2 = add_version(db, raw, KGI + b"\n% v2", 2, prev=v1)    # 新版（位元組不同、條文相同）
    process_pending(ctx, [ClauseHandler()], on_event=lambda e, s, sm: seen.append(sm))
    assert seen[-1]["diff"] == {"added": [], "removed": [], "changed": []}
    row = db.conn.execute("SELECT * FROM clause_diffs WHERE raw_doc_id=?", (v2,)).fetchone()
    assert row["previous_raw_doc_id"] == v1
    assert db.conn.execute("SELECT clause_version FROM product_terms").fetchone()[0] == 2

    # 抓到別的商品的條款（M3 對應錯誤）：標警告，product_terms 不採用它的欄位
    v3 = add_version(db, raw, CARDIF, 3, prev=v2)
    process_pending(ctx, [ClauseHandler()], on_event=lambda e, s, sm: seen.append(sm))
    assert seen[-1]["not_main_clause"] and any("條款與商品不符" in w for w in seen[-1]["warnings"])
    t = db.conn.execute("SELECT clause_raw_doc_id, coverage FROM product_terms").fetchone()
    assert t["clause_raw_doc_id"] is None and t["coverage"] is None


def test_enricher_can_only_fill_missing_fields_with_evidence(tmp_path):
    db, raw = setup_db(tmp_path)

    class FakeLLM(Enricher):
        name = "fake"

        def enrich_clause(self, fields, missing, articles):
            return {"issue_age": {"value": "0–75 歲", "evidence": {"article_no": 2}},
                    "currency": {"value": "JPY", "evidence": {"article_no": 3}},      # 不在 missing，不得覆蓋
                    "riders": {"value": "無"}}                                       # 沒有出處，不收
    ctx = ParseContext(db=db, raw=raw, enricher=FakeLLM())
    rid = add_version(db, raw, KGI, 1)
    process_pending(ctx, [ClauseHandler()])
    row = db.conn.execute("SELECT * FROM clause_docs WHERE raw_doc_id=?", (rid,)).fetchone()
    fields, missing = json.loads(row["fields"]), json.loads(row["missing"])
    assert fields["issue_age"] == "0–75 歲" and fields["currency"] == "USD"
    assert "riders" in missing and "issue_age" not in missing
    assert json.loads(row["evidence"])["issue_age"]["by"] == "fake"
    assert row["parser"] == "clause-rules-v1+fake"


def test_endorsement_pdf_flagged_not_main_clause():
    """台灣人壽有商品被對應到「投資標的…批註條款」：要標出來，不能把它當成主約條款的欄位。"""
    r = parse_clause_pdf(fixture_bytes("companies", "taiwanlife", "endorsement_sample.pdf"), "台灣人壽鑫豐收外幣變額年金保險")
    assert r.not_main_clause and r.fields == {}
    assert any("批註條款" in w for w in r.warnings)


def test_source_correction_is_not_diffed(tmp_path):
    """D25：url_changed 的 doc_revised 是來源更正，不做逐條比對、不寫 clause_diffs。"""
    db, raw = setup_db(tmp_path)
    ctx = ParseContext(db=db, raw=raw)
    v1 = add_version(db, raw, KGI, 1)
    add_version(db, raw, KGI + b"\n% fixed", 2, prev=v1, extra={"url_changed": True, "previous_url": "https://old"})
    seen = []
    process_pending(ctx, [ClauseHandler()], on_event=lambda e, s, sm: seen.append(sm))
    assert seen[-1]["source_corrected"] and "diff" not in seen[-1]
    assert db.conn.execute("SELECT COUNT(*) FROM clause_diffs").fetchone()[0] == 0
