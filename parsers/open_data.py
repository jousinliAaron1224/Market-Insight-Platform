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
import io
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
