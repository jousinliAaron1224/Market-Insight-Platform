"""開放資料統計表解析與需求面結論（parsers/open_data.py、web/market.py）。"""
import pytest

from parsers.open_data import parse_lia_14539, parse_tii_i10
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
