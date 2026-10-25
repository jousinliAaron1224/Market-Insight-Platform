from datetime import date
from types import SimpleNamespace

from core.storage import Database
from scripts.export_radar import spec_changes, spec_news

NAME = "國泰人壽測試變額萬能壽險"


def _fees(front, surr):
    return f"【附表】費用一覽表 一、保費費用 {front}% 二、保單管理費 每月 100 元 解約費用率 第一年 {surr}% 部分提領費用 無"


def _add_version(db, version, fetched, preamble, appendix, key="company_cathay_products:" + NAME, parser="clause-rules-v1"):
    cur = db.conn.execute("""INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
                             VALUES ('company_cathay_products', ?, ?, ?, 'x', ?, 'http://x/clause.pdf', 'pdf', '{}')""", (key, version, f"h{key}{version}", fetched))
    rid = cur.lastrowid
    db.conn.execute("""INSERT INTO clause_docs (raw_doc_id, source_id, item_key, company, product_name, version, parser, parsed_at)
                       VALUES (?, 'company_cathay_products', ?, '國泰人壽', ?, ?, ?, ?)""", (rid, key, NAME, version, parser, fetched))
    for seq, (kind, title, text) in enumerate([("preamble", None, preamble), ("appendix", "附表", appendix)]):
        db.conn.execute("""INSERT INTO clause_articles (raw_doc_id, seq, kind, article_no, title, text, text_hash) VALUES (?, ?, ?, NULL, ?, ?, 'h')""",
                        (rid, seq, kind, title, text))


def _db(tmp_path):
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    db.conn.execute("""INSERT INTO product_terms (company, name, line, status, updated_at, clause_url) VALUES ('國泰人壽', ?, '變額萬能壽險', 'on_sale', '2026-10-05', 'http://x/latest.pdf')""", (NAME,))
    return db


def test_real_revision_produces_fee_change(tmp_path):
    db = _db(tmp_path)
    _add_version(db, 1, "2026-09-01T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(3, 7))
    _add_version(db, 2, "2026-10-05T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(1.5, 7))
    db.conn.commit()
    ch, skipped = spec_changes(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    assert skipped == [] and len(ch) == 1
    assert [(lab, a, b) for lab, a, b, _ in ch[0]["changes"]] == [("前置費用", "3%", "1.5%")]
    news = spec_news(ch, {NAME: "P401"}, date(2026, 10, 20))
    assert len(news) == 1 and news[0]["imp"] == 4 and news[0]["cat"] == "競品新商品"
    assert "前置費用 3% → 1.5%" in news[0]["t"] and news[0]["prods"] == ["P401"] and news[0]["d"] == "2026-10-05"


def test_wrong_document_is_not_a_revision(tmp_path):
    db = _db(tmp_path)   # 第一版抓到的是批註條款（前言沒有商品名稱）→ 不能當成改版
    _add_version(db, 1, "2026-10-03T02:00:00+00:00", "國泰人壽全權委託帳戶資產撥回投資標的批註條款", _fees(0, 10))
    _add_version(db, 2, "2026-10-05T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(3, 7))
    db.conn.commit()
    ch, skipped = spec_changes(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    assert ch == [] and len(skipped) == 1 and "不是同一份" in skipped[0][2]


def test_unchanged_and_old_changes_are_quiet(tmp_path):
    db = _db(tmp_path)
    _add_version(db, 1, "2026-01-01T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(3, 7))
    _add_version(db, 2, "2026-02-01T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(3, 7))
    db.conn.commit()
    ch, skipped = spec_changes(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    assert ch == [] and skipped == []
    _add_version(db, 3, "2026-02-10T02:00:00+00:00", f"國泰人壽 {NAME}", _fees(2, 7))
    db.conn.commit()
    ch, _ = spec_changes(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    assert len(ch) == 1 and spec_news(ch, {}, date(2026, 10, 20)) == []   # 改版發生在 90 天前，不進情報
