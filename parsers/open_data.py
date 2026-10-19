"""開放資料統計表的解析（D28 市場數據）。檔案很小，頁面要用時直接從 raw 讀、不另建資料表。

- tii_i10：保險事業發展中心「人身保險業保費收入」（data.gov.tw 104113；openapi.tii.org.tw TableID=I10）
  依險種、依月份的全市場保費收入（百萬元），2008 年起；年度列的「月」是空的。
  注意：是「保費收入」（含續期保費），不是新契約；也沒有公司別、沒有投資型拆分。
- lia_14539：壽險公會「壽險業績統計」（data.gov.tw 14539）
  實際內容只有一期（初／續年度 × 投資型／傳統型），公告日期 2020-03-25，已多年未更新。

欄位與預期不同時丟 ValueError 並列出實際欄位，方便資料提供者改版時調整。
"""
from __future__ import annotations

import csv
import json
import io
import re
from typing import Any

I10_LINES = {
    "個人人壽保險_百萬元": "個人壽險",
    "個人年金保險_百萬元": "個人年金",
    "個人健康保險_百萬元": "個人健康",
    "個人傷害保險_百萬元": "個人傷害",
}
I10_GROUP = ["團體人壽保險_百萬元", "團體健康保險_百萬元", "團體傷害保險_百萬元", "團體年金保險_百萬元"]
LINES = ["個人壽險", "個人年金", "個人健康", "個人傷害", "團體保險"]


def _text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("無法辨識檔案編碼（不是 UTF-8 也不是 Big5）")


def _num(s: str) -> float | None:
    s = (s or "").replace(",", "").strip()
    if not s or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_tii_i10(data: bytes) -> dict[str, Any]:
    """→ {annual:[{year, total, yoy, lines{}, announced}], monthly:[{month:'YYYY-MM', ...}]}"""
    rows = list(csv.reader(io.StringIO(_text(data))))
    if not rows:
        raise ValueError("I10：檔案是空的")
    head = [h.strip() for h in rows[0]]
    need = ["年", "月", "總計_百萬元", "年月增率", *I10_LINES, *I10_GROUP]
    missing = [n for n in need if n not in head]
    if missing:
        raise ValueError(f"I10：缺欄位 {missing}；實際欄位：{head}")
    ix = {h: i for i, h in enumerate(head)}
    annual, monthly = [], []
    for r in rows[1:]:
        if len(r) < len(need):
            continue
        y, m = r[ix["年"]].strip(), r[ix["月"]].strip()
        total = _num(r[ix["總計_百萬元"]])
        if not (y.isdigit() and len(y) == 4) or total is None:   # 註、資料來源、雜訊列
            continue
        lines = {zh: _num(r[ix[col]]) or 0.0 for col, zh in I10_LINES.items()}
        lines["團體保險"] = sum(_num(r[ix[c]]) or 0.0 for c in I10_GROUP)
        rec = {"total": total, "yoy": _num(r[ix["年月增率"]]), "lines": lines,
               "announced": r[ix["公告日期"]].strip() if "公告日期" in ix else None}
        if m:
            if not m.isdigit() or not 1 <= int(m) <= 12:
                continue
            monthly.append({"month": f"{y}-{int(m):02d}", **rec})
        else:
            annual.append({"year": int(y), **rec})
    if not monthly:
        raise ValueError("I10：沒有任何月資料列")
    monthly.sort(key=lambda x: x["month"])
    annual.sort(key=lambda x: x["year"])
    return {"annual": annual, "monthly": monthly}


def parse_lia_14539(data: bytes) -> list[dict[str, Any]]:
    """→ [{announced, item:'初年度投資型', last, this, growth}]"""
    rows = list(csv.DictReader(io.StringIO(_text(data))))
    need = {"公告日期", "去年度保費收入", "本年度保費收入", "項目"}
    if not rows or not need <= set(rows[0]):
        raise ValueError(f"14539：欄位不符；實際欄位：{list(rows[0]) if rows else []}")
    out = []
    for r in rows:
        item = (r["項目"] or "").replace("/度", "年度").strip()      # 原檔「初/度投資型」
        out.append({"announced": r["公告日期"].strip(), "item": item,
                    "last": _num(r["去年度保費收入"]), "this": _num(r["本年度保費收入"]),
                    "growth": _num(r.get("成長率_%", ""))})
    return out


# ---------------------------------------------------------------------- 7191 壽險財務業務指標
# 保險局「保險業公開資訊觀測站」的開放資料（ins-info.ib.gov.tw/opendata/json-06161610.aspx）。
# 欄位是 AMOUNT1、AMOUNT2…，沒有欄名；依資料集說明列出的順序對應（下載後以數值範圍核對過才使用）。
IB_7191_FIELDS = [
    "負債占資產比率", "各種責任準備金對資產比率", "各種責任準備金變動率", "各種責任準備金淨增額對保費收入比率",
    "關係企業投資額對業主權益比率", "初年度保費比率", "續年度保費比率", "新契約費用率", "保費收入變動率",
    "業主權益變動率", "淨利變動率", "資金運用比率", "繼續率(十三個月)", "繼續率(二十五個月)", "資產報酬率",
    "業主權益報酬率", "資金運用淨收益率", "投資報酬率", "營業利益對營業收入比率", "稅前純益對總收入比率",
    "純益率", "每股盈餘", "不動產投資與不動產抵押放款對資產比率",
]


def parse_ib_7191(data: bytes) -> list[dict[str, Any]]:
    """→ [{period:'2026Q2', roc_year, quarter, company, values:{指標: float|None}}]"""
    doc = json.loads(_text(data))
    if isinstance(doc, dict):                      # 有些平臺會包一層
        doc = next((v for v in doc.values() if isinstance(v, list)), [])
    if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
        raise ValueError("7191：不是預期的 JSON 陣列")
    need = {"ClaimYear", "ClaimQuarter", "INSURER_Name"}
    if not need <= set(doc[0]):
        raise ValueError(f"7191：缺欄位 {sorted(need - set(doc[0]))}；實際欄位：{sorted(doc[0])[:40]}")
    out = []
    for r in doc:
        y, q = str(r.get("ClaimYear", "")).strip(), str(r.get("ClaimQuarter", "")).strip()
        if not (y.isdigit() and q.isdigit()):
            continue
        vals = {name: _num(str(r.get(f"AMOUNT{i}", "") or "").replace("N/A", ""))
                for i, name in enumerate(IB_7191_FIELDS, start=1)}
        out.append({"period": f"{int(y) + 1911}Q{int(q)}", "roc_year": int(y), "quarter": int(q),
                    "company": str(r["INSURER_Name"]).strip(), "values": vals})
    if not out:
        raise ValueError("7191：沒有任何資料列")
    return out


# ---------- 目標客群（2026-10-19）：聯徵中心房貸、國發會人口年齡結構 ----------
def _rows(data: bytes) -> list[dict[str, str]]:
    """CSV → 每列一個 dict；欄名去掉 BOM、引號與空白。"""
    rows = list(csv.reader(io.StringIO(_text(data))))
    if not rows:
        return []
    head = [h.replace("﻿", "").strip().strip('"') for h in rows[0]]
    return [dict(zip(head, (c.strip() for c in r))) for r in rows[1:] if len(r) >= len(head) and any(r)]


def _col(head: list[str], *keys: str) -> str:
    """找第一個包含所有關鍵字的欄名；找不到就報錯並列出實際欄名（格式改了才看得出來）。"""
    for h in head:
        if all(k in h for k in keys):
            return h
    raise ValueError(f"找不到欄位 {keys}；實際欄位：{head}")


def parse_jcic_mortgage_age(data: bytes) -> list[dict[str, Any]]:
    """聯徵中心「房貸借款人各年齡層下的新增授信金額及利率」（data.gov.tw 21946，每月）
    → [{m:'YYYY-MM', age:'30-40', n:人數, amt:新增授信（百萬元）, rate:平均利率%}]"""
    rows = _rows(data)
    if not rows:
        return []
    h = list(rows[0])
    cy, cm, ca, cn, cv, cr = _col(h, "年"), _col(h, "月"), _col(h, "年齡"), _col(h, "人數"), _col(h, "授信"), _col(h, "利率")
    out = []
    for r in rows:
        n, v, rt = _num(r[cn]), _num(r[cv]), _num(r[cr])
        if n is None or not r[cy].isdigit():
            continue
        out.append({"m": f"{int(r[cy])}-{int(r[cm]):02d}", "age": r[ca], "n": int(n), "amt": round((v or 0) / 1000, 1),
                    "rate": round(rt, 3) if rt is not None else None})
    return out


def parse_jcic_mortgage_gender(data: bytes) -> list[dict[str, Any]]:
    """聯徵中心「購置住宅貸款統計分析表-性別（流量）」（data.gov.tw 113748，每季）
    → [{q:'YYYY-MM', sex:'男'|'女', n, limit:平均授信額度（千元）, value:平均鑑估值（千元）, ltv:核貸成數加權平均%, rate, area:平均建物面積 m²}]"""
    rows = _rows(data)
    if not rows:
        return []
    h = list(rows[0])
    c = {"y": _col(h, "年"), "m": _col(h, "月"), "sex": _col(h, "性別"), "n": _col(h, "樣本數"), "limit": _col(h, "授信額度"),
         "value": _col(h, "鑑估值"), "ltv": _col(h, "核貸成數", "加權"), "rate": _col(h, "利率"), "area": _col(h, "面積")}
    out = []
    for r in rows:
        if not r[c["y"]].isdigit():
            continue
        out.append({"q": f"{int(r[c['y']])}-{int(r[c['m']]):02d}", "sex": {"1": "男", "2": "女"}.get(r[c["sex"]], r[c["sex"]]),
                    **{k: _num(r[c[k]]) for k in ("n", "limit", "value", "ltv", "rate", "area")}})
    return out


def parse_jcic_loan_age(data: bytes) -> list[dict[str, Any]]:
    """聯徵中心「不同年齡及貸款類別區隔下授信金額及逾放比」（data.gov.tw 151726，每年）
    → [{y, sex, age, type:'房貸'…, n:人數, limit:額度（百萬元）, bal:餘額（百萬元）, npl:逾放比}]"""
    out = []
    for r in _rows(data):
        if not r.get("年", "").isdigit():
            continue
        t = re.sub(r"^\d+\.", "", r.get("授信類別", "")).replace("(含修繕)", "")
        out.append({"y": int(r["年"]), "sex": {"1": "男", "2": "女"}.get(r.get("性別"), r.get("性別")), "age": r.get("年齡"), "type": t,
                    "n": _num(r.get("人數")), "limit": _num(r.get("額度")), "bal": _num(r.get("餘額")), "npl": _num(r.get("逾期放款比率"))})
    return out


def parse_ndc_population(data: bytes) -> list[dict[str, Any]]:
    """國發會「1-1 土地面積、戶數、人口數、人口密度、年齡結構及扶養比例」（data.gov.tw 6326，每年）
    → [{y, area, pop, households, p0_14, p15_64, p65（%）, dep:扶養比%}]"""
    rows = _rows(data)
    if not rows:
        return []
    h = list(rows[0])
    c = {"pop": next(x for x in h if x == "年底人口數"), "hh": _col(h, "戶數"), "p0": _col(h, "0~14", "百分比"),
         "p15": _col(h, "15~64", "百分比"), "p65": _col(h, "65", "百分比"), "dep": _col(h, "扶養")}
    out = []
    for r in rows:
        if not r.get("年", "").isdigit():
            continue
        out.append({"y": int(r["年"]), "area": r["地區"], "pop": _num(r[c["pop"]]), "households": _num(r[c["hh"]]),
                    "p0_14": round(_num(r[c["p0"]]) or 0, 2), "p15_64": round(_num(r[c["p15"]]) or 0, 2),
                    "p65": round(_num(r[c["p65"]]) or 0, 2), "dep": round(_num(r[c["dep"]]) or 0, 2)})
    return out
