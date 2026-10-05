"""開放資料統計表解析與需求面結論（parsers/open_data.py、web/market.py）。"""
import pytest

from parsers.open_data import IB_7191_FIELDS, parse_ib_7191, parse_lia_14539, parse_tii_i10
from tests.conftest import fixture_bytes
from web.market import _demand_from_i10

I10 = fixture_bytes("open_data", "tii_i10_trimmed.csv")


def test_i10_rows():
    t = parse_tii_i10(I10)
    assert t["monthly"][-1]["month"] == "2026-06" and t["monthly"][-1]["total"] == 317926
    assert t["monthly"][-1]["lines"]["個人年金"] == 56362
    assert t["monthly"][-1]["lines"]["團體保險"] == 569 + 1238 + 813 + 26
    years = [a["year"] for a in t["annual"]]
    assert years[-1] == 2025 and 2026 not in years            # 今年還沒有年度列
    assert all(m["month"][:4].isdigit() for m in t["monthly"])  # 註、資料來源、雜訊列都跳過


def test_i10_big5_and_missing_column():
    t = parse_tii_i10(I10.decode("utf-8-sig").encode("cp950"))
    assert t["monthly"][-1]["month"] == "2026-06"
    with pytest.raises(ValueError, match="缺欄位"):
        parse_tii_i10("年,月,總計\n2026,01,1\n".encode())


def test_demand_ytd_and_conclusions():
    d = _demand_from_i10(parse_tii_i10(I10))
    assert d["ytd_label"] == "2026 年 1–6 月" and len(d["monthly"]) == 24
    ann = next(r for r in d["ytd"] if r["line"] == "個人年金")
    assert (ann["this"], ann["last"], ann["growth"]) == (2792.7, 1371.8, 103.6)
    assert d["conclusions"][0].startswith("2026 年 1–6 月人身保險業保費收入 16,311 億元")
    assert "成長最快的是個人年金" in d["conclusions"][1]


def test_lia_14539():
    rows = parse_lia_14539(fixture_bytes("open_data", "lia_14539.csv"))
    assert [r["item"] for r in rows] == ["初年度投資型", "初年度傳統型", "續年度投資型", "續年度傳統型"]
    assert rows[0]["this"] == 275993 and rows[0]["growth"] == 52.2


def test_ib_7191_synthetic():
    rows = parse_ib_7191(fixture_bytes("open_data", "ib_7191_synthetic.json"))
    assert len(IB_7191_FIELDS) == 23 and len(rows) == 16
    r = next(r for r in rows if r["period"] == "2026Q2" and r["company"].startswith("臺銀"))
    assert r["values"]["新契約費用率"] is None and r["values"]["保費收入變動率"] == -3.0
    with pytest.raises(ValueError, match="缺欄位"):
        parse_ib_7191(b'[{"a": 1}]')


def test_company_conclusions_and_rank_direction():
    import sqlite3
    from web.market import companies
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""CREATE TABLE raw_docs (source_id, item_key, version, raw_path, fetched_at, meta);
                          CREATE TABLE product_terms (company);
                          INSERT INTO product_terms VALUES ('法商法國巴黎人壽'), ('富邦人壽');""")
    conn.execute("INSERT INTO raw_docs VALUES ('open_data','life_indicators:0',1,'x','2026-10-05','{\"dataset\": \"life_indicators\"}')")
    c = companies(conn, lambda _: fixture_bytes("open_data", "ib_7191_synthetic.json"), "法商法國巴黎人壽")
    ind = {i["name"]: i for i in c["indicators"]}
    assert ind["新契約費用率"]["values"]["法商法國巴黎人壽"]["rank"] == 1      # 費用率越低越前面（6% 最低；臺銀 N/A 不算）
    assert c["conclusions"][0].startswith("2026Q2 法商法國巴黎人壽保費收入變動率 +85.0%，在 8 家壽險公司中排第 1")
