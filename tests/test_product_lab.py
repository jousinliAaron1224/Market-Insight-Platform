"""商品工作台（web/product_lab.py）：競品欄位擷取、檢查提示、草案儲存。"""
import json

from web.product_lab import (DraftStore, checks, clean_data, cn2int, draft_from_product, extract_features,
                             load_modules, summarize)

KGI_APPENDIX = """附表二、相關費用一覽表
費 用 項 目 收取標準及說明
一、保 費 費 用 無
二、保單管理費 每保單週月日以保單帳戶價值的百分之零點零八計算。
四、解約及部分提領費用
(1)目標保費保單帳戶解約費用:解約費用為「…」×「…解約費用率」。
第1次 第1至2次 第1至4次 第1至12次 10%
第2次 第3至4次 第5至8次 第13至24次 9%
第9次以上 第17次以上 第33次以上 第97次以上 0%
(2)超額保費保單帳戶解約費用:
附表四、加值回饋金表
第8年以上 2%
全權委託管理帳戶詳如凱基人壽全委帳戶投資標的批註條款(二)附表二。"""

CATHAY_APPENDIX = """費用項目收取標準
未達15萬 4%
一、保費費用
15萬(含)以上 3.8%
二、保單管理費:無
四、解約及部分提領費用
1.解約費用 無
投資標的名稱 撥回率或撥回金額非固定"""

DEFS = "四、保證期間:係指…。若要保人於投保時選擇分期給付者,應另選擇「保證期間」為十年、十五年或二十年。\n三、年金累積期間:…,該期間不得低於六年。"


def arts(appendix, defs=DEFS, titles=("保單帳戶價值的部分提領", "名詞定義")):
    out = [{"kind": "article", "title": t, "text": defs if t == "名詞定義" else "…"} for t in titles]
    return out + [{"kind": "appendix", "title": None, "text": appendix}]


def prod(name="凱基人壽享鑽年年變額年金保險(112)", line="變額年金保險", currency="TWD"):
    return {"name": name, "line": line, "currency": currency, "payment_modes": json.dumps(["彈性繳", "月繳"]),
            "coverage": json.dumps(["返還保單帳戶價值", "年金"]), "riders": None}


def test_cn2int():
    assert [cn2int(x) for x in ("十", "十五", "二十", "五", "20", "二十五", "百")] == [10, 15, 20, 5, 20, 25, None]


def test_extract_kgi_style():
    f = extract_features(prod(), arts(KGI_APPENDIX))
    assert f["premium_charge_pct"] == 0.0                # 「保 費 費 用 無」：中文字間的空白要能容忍
    assert f["surrender_max_pct"] == 10.0
    assert f["discretionary"] and f["bonus"] and not f["distribution"]
    assert f["annuity_guarantee_years"] == ["10", "15", "20"] and f["accumulation_min_years"] == 6
    assert f["payment_modes"] == ["不定期繳／彈性繳", "定期繳"] and f["currency"] == ["新臺幣"]
    assert f["death_type"] == ["返還保單帳戶價值（年金型）"] and f["partial_withdrawal"]


def test_extract_cathay_style_and_life_types():
    f = extract_features(prod("國泰人壽鑫超澳利富外幣變額年金保險", currency="AUD"), arts(CATHAY_APPENDIX))
    assert f["premium_charge_pct"] == 4.0 and f["surrender_max_pct"] == 0.0 and f["distribution"]
    assert f["currency"] == ["澳幣"]
    life = extract_features(prod("某某變額萬能壽險", line="變額萬能壽險"),
                            arts("", defs="一、保險金額:甲型:…乙型:…"))
    assert life["death_type"] == ["甲型（保額與帳戶價值取大）", "乙型（保額加帳戶價值）"]
    assert life["premium_charge_pct"] is None              # 沒有附表就是擷取不到


def test_numbers_not_glued_across_spaces():
    f = extract_features(prod(), arts("一、保費費用 依保費級距 500 3.2% 1000 2.5%\n二、保單管理費"))
    assert f["premium_charge_pct"] == 3.2


def test_summarize_counts_and_range():
    items = [{"company": c, "features": {"x": v, "y": n}} for c, v, n in
             (("A", ["躉繳"], 1.0), ("B", ["躉繳", "定期繳"], 3.0), ("A", None, 2.0))]
    s = summarize(items, "x", "A")
    assert s["kind"] == "counts" and s["counts"][0] == ["躉繳", 2, 1] and s["n"] == 2
    r = summarize(items, "y", "A")
    assert (r["min"], r["median"], r["max"]) == (1.0, 2.0, 3.0) and r["by_company"]["A"] == {"min": 1.0, "max": 2.0, "n": 2}


def test_checks():
    mods = load_modules()
    assert [m["id"] for m in mods][:2] == ["positioning", "underwriting"] and len(mods) == 9
    feats = [{"company": "A", "line": "變額壽險", "features": {"premium_charge_pct": 3.0, "surrender_max_pct": 5.0}}]
    c = checks({"fields": {"line": "變額壽險", "age_max": 70, "premium_charge": 4, "currency": ["美元"],
                           "distribution": True, "channel": ["網路投保"]}}, mods, feats)
    msgs = " ".join(x["msg"] for x in c)
    assert "65 歲以上" in msgs and "最低比率" in msgs and "高於同險種競品最高 3%" in msgs
    assert "匯率" in msgs and "撥回" in msgs and "網路投保" in msgs
    assert any(x["level"] == "missing" and "繳費方式" in x["msg"] for x in c)
    assert not any("還沒填" in x["msg"] and "險種" in x["msg"] for x in c)


def test_draft_store(tmp_path):
    st = DraftStore(tmp_path / "drafts.db")
    d = st.create("  ", {"fields": {"line": "變額壽險", "bad": {"x": 1}}, "notes": {"fees": "n"}, "junk": 1})
    assert d["name"] == "未命名商品" and d["fields"] == {"line": "變額壽險"} and d["notes"] == {"fees": "n"}
    u = st.update(d["id"], "新名稱", {"fields": {"age_max": 70}, "refs": {"fees": [{"kind": "reg", "title": "t"}, "x"]}})
    assert u["name"] == "新名稱" and u["fields"] == {"age_max": 70} and u["refs"] == {"fees": [{"kind": "reg", "title": "t"}]}
    assert [x["id"] for x in st.list()] == [d["id"]]
    assert st.delete(d["id"]) and st.get(d["id"]) is None and st.update(999, None, {}) is None
    assert clean_data({"fields": "x"}) == {"fields": {}, "notes": {}, "refs": {}}


def test_draft_from_product():
    item = {"company": "凱基人壽", "name": "N", "pdf": "/raw/x.pdf",
            "features": extract_features(prod(), arts(KGI_APPENDIX))}
    d = draft_from_product(item)
    assert d["fields"]["line"] == "變額年金保險" and d["fields"]["surrender_max"] == 10.0
    assert d["refs"]["positioning"][0]["url"] == "/raw/x.pdf"
