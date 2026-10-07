from scripts.export_radar import mentioned, pct


def test_mentioned_finds_company_ids_once():
    assert mentioned("台灣人壽保險股份有限公司違反保險法令裁罰案") == ["taiwanlife"]
    assert mentioned("法國巴黎人壽與法巴人壽、國泰人壽") == ["cardif", "cathay"]
    assert mentioned("金管會提醒民眾投保登山險") == []


def test_pct_keeps_integers_clean():
    assert pct(None) is None
    assert pct(5.0) == 5
    assert pct(2.456) == 2.46


def test_shelf_changes_skip_baseline(tmp_path):
    from datetime import date
    from types import SimpleNamespace
    from core.storage import Database
    from scripts.export_radar import shelf, shelf_news
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    rows = [  # (insurer, product, line, first_seen, removed_at, baseline)
        ("法國巴黎人壽", "價值大師外幣變額萬能壽險", "investment", "2026-09-01T02:00:00+00:00", None, 1),   # 基準：不算上架
        ("安聯人壽", "美利旺分紅終身壽險", "participating", "2026-10-03T02:00:00+00:00", None, 0),       # 新上架
        ("富邦人壽", "某某終身壽險", "other", "2026-09-01T02:00:00+00:00", "2026-10-04T02:00:00+00:00", 1),  # 下架
        ("凱基人壽", "很久以前上架", "other", "2026-07-01T02:00:00+00:00", None, 0),                   # 超過 30 天
    ]
    db.conn.executemany("""INSERT INTO bank_shelf (bank_id, item_key, insurer, product, line, category, currency,
                           first_seen, removed_at, baseline) VALUES ('b_mega','b_mega:all',?,?,?,'','',?,?,?)""", rows)
    db.conn.commit()
    cfg = {"sources": [{"id": "bank_shelf", "banks": [{"id": "b_mega", "name": "兆豐銀行"}]}]}
    data, changes = shelf(SimpleNamespace(db_path=str(tmp_path / "intel.db")), cfg, date(2026, 10, 5))
    bank = data["banks"][0]
    assert bank["name"] == "兆豐銀行" and bank["count"] == 3
    assert bank["by_company"] == {"cardif": 1, "allianz": 1, "kgi": 1}
    assert sorted((c["kind"], c["co"]) for c in changes) == [("上架", "allianz"), ("下架", "fubon")]
    news = shelf_news(changes, data["banks"])
    up = next(n for n in news if "上架" in n["t"])
    assert up["t"] == "兆豐銀行上架安聯人壽「美利旺分紅終身壽險」" and up["lines"] == ["par"] and up["imp"] == 4
    assert up["cat"] == "通路動態" and up["co"] == ["allianz"]


def test_rates_distribution_moves_and_fixed_panel(tmp_path):
    from types import SimpleNamespace
    from core.storage import Database
    from scripts.export_radar import rates
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    months = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    rows = []
    for m in months:
        rows.append(("cathay", "A", "國泰人壽甲美元利率變動型終身壽險", m, 4.0))
        rows.append(("cathay", "B", "國泰人壽乙美元利率變動型終身壽險", m, 4.35 if m == "2026-10" else 4.30))   # 10 月調升
        rows.append(("cathay", "C", "國泰人壽丙美元利率變動型年金保險", m, 3.0 if m < "2026-10" else 2.9))      # 10 月調降
        if m >= "2026-09":
            rows.append(("cathay", "NEW", "國泰人壽新美元利率變動型終身壽險", m, 5.0))   # 新商品：不進固定樣本
    db.conn.executemany("""INSERT INTO declared_rates (company, product_code, product_name, month, rate_pct, currency, line, source_url)
                           VALUES (?,?,?,?,?,'USD','usd_interest','https://x')""", rows)
    db.conn.commit()
    cfg = {"sources": [{"id": "declared_rates", "companies": [{"id": "cathay", "name": "國泰人壽", "page_url": "https://c"}]}]}
    out = rates(SimpleNamespace(db_path=str(tmp_path / "intel.db")), cfg)
    assert out["months"] == months[1:] and out["latest"] == "2026-10" and out["prev"] == "2026-09"
    d = out["dist"][0]
    assert (d["n"], d["min"], d["max"]) == (4, 2.9, 5.0) and d["med"] == round((4.0 + 4.35) / 2, 4)
    assert out["moves"]["cathay"] == {"up": 1, "down": 1, "same": 2}
    assert out["market"] == {"n": 4, "med": d["med"]}
    assert out["last_change"] == "2026-10" and out["history"][-1]["by"] == {"cathay": {"up": 1, "down": 1, "steps": {"+0.05": 1, "-0.10": 1}}}
    assert all(h["up"] == h["down"] == 0 for h in out["history"][:-1])
    assert [(m["name"], m["from"], m["to"]) for m in out["movers"]] == [
        ("丙美元利率變動型年金保險", 3.0, 2.9), ("乙美元利率變動型終身壽險", 4.3, 4.35)]
    s = out["series"][0]
    assert s["n"] == 3 and s["v"][-1] == 4.0 and s["v"][0] == 4.0       # 中位數只看 A、B、C，不受 NEW 影響


def test_rate_news_per_company_month_and_flat_summary():
    from scripts.export_radar import rate_news
    r = {"latest": "2026-10", "last_change": "2026-07", "market": {"n": 3, "med": 4.0},
         "companies": [{"co": "cathay", "name": "國泰人壽", "source": "https://c"}],
         "moves": {"cathay": {"up": 0, "down": 0, "same": 3}},
         "history": [{"m": "2026-07", "up": 2, "down": 0, "by": {"cathay": {"up": 2, "down": 0, "steps": {"+0.05": 2}}}},
                     {"m": "2026-08", "up": 0, "down": 0, "by": {}}]}
    out = rate_news(r)
    assert [n["t"] for n in out] == ["國泰人壽 7 月美元利變商品宣告利率調升 2 張", "10 月美元利變宣告利率：1 家公司 3 張商品全數持平"]
    assert out[0]["d"] == "2026-07-01" and out[0]["cat"] == "宣告利率" and out[0]["url"] == "https://c"
    assert rate_news(None) == []


def test_product_news_groups_recent_approvals():
    from datetime import date
    from scripts.export_radar import product_news
    ps = [{"id": "P1", "co": "kgi", "short": "甲", "sub": "變額壽險", "status": "新上市", "versions": [{"d": "2026-09-01"}], "src": "u"},
          {"id": "P2", "co": "kgi", "short": "乙", "sub": "變額年金保險", "status": "新上市", "versions": [{"d": "2026-09-01"}], "src": "u"},
          {"id": "P3", "co": "cathay", "short": "舊", "status": "銷售中", "versions": [{"d": "2025-01-01"}]},
          {"id": "P4", "co": "cardif", "short": "丙", "status": "停售", "versions": [{"d": "2026-09-20"}]}]
    out = product_news(ps, date(2026, 10, 7))
    assert len(out) == 1 and out[0]["t"] == "凱基人壽新核准 2 張投資型商品：甲、乙"
    assert out[0]["cat"] == "競品新商品" and out[0]["prods"] == ["P1", "P2"] and out[0]["prodCount"] == 2


def test_shelf_products_merge_banks_and_attach_declared_rate(tmp_path):
    from types import SimpleNamespace
    from core.storage import Database
    from scripts.export_radar import shelf_products
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    db.conn.execute("""INSERT INTO declared_rates (company, product_code, product_name, month, rate_pct, currency, line, source_url)
                       VALUES ('kgi','X','凱基人壽享美鑫美元利率變動型終身壽險－定期給付型','2026-10',4.35,'USD','usd_interest','https://k')""")
    db.conn.commit()
    item = lambda insurer, product, line, cur: {"co": None, "insurer": insurer, "product": product, "line": line, "category": "壽險", "currency": cur, "removed": None}  # noqa: E731
    shelf = {"banks": [
        {"id": "b_mega", "name": "兆豐銀行", "items": [dict(item("凱基人壽", "享美鑫美元利率變動型終身壽險-定期給付型", "usd", "美元"), co="kgi"),
                                                     item("新光人壽", "美鴻世代美元分紅終身壽險", "par", "美元")]},
        {"id": "b_hncb", "name": "華南銀行", "items": [dict(item("凱基人壽", "享美鑫美元利率變動型終身壽險–定期給付型", "usd", ""), co="kgi"),
                                                     dict(item("某人壽", "健康險", "health", ""), co=None)]}]}
    cfg = {"sources": [{"id": "declared_rates", "companies": [{"id": "kgi", "page_url": "https://k"}]}]}
    out = shelf_products(SimpleNamespace(db_path=str(tmp_path / "intel.db")), cfg, shelf)
    kgi = next(p for p in out if p["co"] == "kgi")
    assert kgi["banks"] == ["兆豐銀行", "華南銀行"] and kgi["cur"] == "USD"          # 兩家銀行寫法不同（- 與 –）仍算同一張
    assert kgi["declared"] == 4.35 and kgi["declaredMonth"] == "2026-10" and kgi["rateSrc"] == "https://k"
    assert next(p for p in out if p["co"] == "skl")["line"] == "par"                # 原本沒有的公司代碼
    assert len(out) == 2 and all(p["real"] and p["shelf"] and not p["auto"] for p in out)


def test_fx_share_reads_latest_press_attachment_table(tmp_path):
    import json as _json
    from types import SimpleNamespace
    from core.storage import Database
    from scripts.export_radar import fx_share
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    meta = {"title": "壽險業115年截至7月底外幣保險商品銷售情形", "published_at": "2026-09-29T00:00:00+08:00",
            "tables": [{"name": "108年至115年外幣保單新契約保費收入占整體新契約保費收入比率", "url": "https://f/a.pdf",
                        "tables": [[["年份", "114/07", "115/07"], ["占比", "40.81%", "35.59%"]]]}]}
    db.conn.execute("""INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
                       VALUES ('fsc_press','k',1,'h','p','2026-09-29T00:00:00+00:00','https://f/news','html',?)""", (_json.dumps(meta, ensure_ascii=False),))
    db.conn.commit()
    out = fx_share(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    assert out["points"] == [{"p": "2025 年 1–7 月", "y": 2025, "fx": 40.81}, {"p": "2026 年 1–7 月", "y": 2026, "fx": 35.59}]
    assert out["url"] == "https://f/news" and out["date"] == "2026-09-29"
