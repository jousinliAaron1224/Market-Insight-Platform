"""法規／新聞 → 對現有商品的影響（D27，規則式、附理由）。"""
from core.config import load_config
from parsers.impact import compute_impact, summary_line

RULES = load_config()["parsing"]["product_impact"]
CARDIF = RULES["self_company"]
P = [
    {"company": CARDIF, "name": "A 變額年金", "line": "變額年金保險", "currency": "TWD", "status": "on_sale"},
    {"company": CARDIF, "name": "B 外幣變額年金", "line": "變額年金保險", "currency": "USD", "status": "on_sale"},
    {"company": CARDIF, "name": "C 變額萬能", "line": "變額萬能壽險", "currency": "TWD", "status": "on_sale"},
    {"company": CARDIF, "name": "D 已停售", "line": "變額壽險", "currency": "TWD", "status": "discontinued"},
    {"company": "國泰人壽", "name": "E", "line": "變額年金保險", "currency": "FX", "status": "on_sale"},
]


def imp(title, src="tii_law_rss"):
    return compute_impact(title, src, P, RULES)


def test_all_investment_products_on_sale_only():
    r = imp("投資型保險商品銷售應注意事項")
    assert r["self"] == ["A 變額年金", "B 外幣變額年金", "C 變額萬能"] and r["competitors"] == {"國泰人壽": 1}
    assert r["scope"] == "全部投資型商品" and "投資型商品規範" in r["reasons"]


def test_narrowing_by_line_and_currency():
    assert imp("投資型年金保險單示範條款")["self"] == ["A 變額年金", "B 外幣變額年金"]
    assert imp("投資型人壽保險單示範條款")["self"] == ["C 變額萬能"]
    r = imp("壽險業外幣保險商品銷售情形")
    assert r["self"] == ["B 外幣變額年金"] and r["competitors"] == {"國泰人壽": 1}
    assert "名詞定義" in imp("投資型年金保險單示範條款")["articles"]


def test_untracked_company_level_and_exclusions():
    r = imp("人身保險業辦理利率變動型保險商品業務應注意事項")
    assert r["scope"] is None and r["untracked"] == ["利率變動型商品"]
    r = imp("中華民國人壽保險商業同業公會所屬會員辦理外幣收付非投資型人身保險業務自律規範")
    assert r["self"] == [] and r["untracked"] == ["外幣非投資型商品"]
    assert imp("保險業自我風險及清償能力評估機制作業規範")["company_level"] == ["資本與清償能力"]
    assert imp("強化財產保險業巨災準備金應注意事項")["scope"] is None
    assert imp("勞保年金改革方案出爐", "news_rss")["scope"] is None          # 不是年金險
    # 同時提到人身保險就不排除
    assert imp("財產保險業及人身保險業辦理資訊公開管理辦法之解釋令")["scope"] == "全部投資型商品"


def test_penalty_is_company_level_only():
    r = imp("某人壽招攬業務員缺失，核處罰鍰", "fsc_penalty")
    assert r["self"] == [] and r["company_level"] == ["同業裁罰：檢視自家同類作業"]
    assert "同業裁罰" in summary_line(r)
