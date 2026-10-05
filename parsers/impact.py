"""法規／新聞 → 對現有商品的影響（規則式，D27）。

只看標題（內文常順帶提到各種名詞，拿來對應商品會太廣）。規則在 sources.yaml 的
parsing.product_impact，每條規則命中時記下理由，結果一定可以解釋：

- scope: all            全部銷售中的投資型商品（M3 收錄範圍）
- line／currency        收斂條件：同類條件取聯集，不同類條件取交集（例如「投資型年金」＝投資型 ∩ 年金）
- untracked             影響的商品線目前沒有收錄（例如利率變動型），照實標出、不硬對應
- company_level         公司層級事項（資本制度、同業裁罰），不對應個別商品
- exclude               命中就不對應任何商品（非投資型、產險、勞保年金…），除非標題也有 exclude_unless（人身保險）
- articles              建議檢視的條文標題（示範條款的標準標題，可直接在競品比較的條文對照打開）

products 由呼叫端傳入（product_terms 的列），這樣新上架的商品會自動納入。
"""
from __future__ import annotations

import re
from typing import Any, Iterable


def _foreign(cur: str | None) -> bool:
    return bool(cur) and cur != "TWD"


def compute_impact(title: str, source_id: str, products: Iterable[dict[str, Any]],
                   rules: dict[str, Any]) -> dict[str, Any]:
    title = title or ""
    self_co = rules.get("self_company", "")
    out: dict[str, Any] = {"reasons": [], "untracked": [], "company_level": [], "articles": [],
                           "self": [], "self_lines": {}, "competitors": {}, "scope": None}
    for pat, titles in (rules.get("articles") or {}).items():
        if re.search(pat, title):
            out["articles"] += [t for t in titles if t not in out["articles"]]

    blocked = [p for p in rules.get("exclude", []) if re.search(p, title)]
    if blocked and any(re.search(p, title) for p in rules.get("exclude_unless", [])):
        blocked = []
    no_products = source_id in (rules.get("no_product_sources") or [])
    any_scope, lines, currency = False, set(), None
    for r in rules.get("rules", []):
        if not re.search(r["match"], title):
            continue
        if r.get("untracked"):
            out["untracked"].append(r["untracked"])
            continue
        if r.get("company_level"):
            out["company_level"].append(r["company_level"])
            continue
        if blocked or no_products:
            continue
        out["reasons"].append(r.get("reason", r["match"]))
        if r.get("scope") == "all":
            any_scope = True
        if r.get("line"):
            lines |= set(r["line"] if isinstance(r["line"], list) else [r["line"]])
        if r.get("currency"):
            currency = r["currency"]
    if blocked:
        out["blocked"] = blocked
    if not (any_scope or lines or currency):
        return out

    desc = []
    if lines:
        desc.append("、".join(sorted(lines)))
    if currency == "foreign":
        desc.append("外幣")
    out["scope"] = ("投資型商品中的 " + "／".join(desc)) if desc else "全部投資型商品"

    for p in products:
        if p.get("status") not in (None, "on_sale"):
            continue
        if lines and p.get("line") not in lines:
            continue
        if currency == "foreign" and not _foreign(p.get("currency")):
            continue
        if p["company"] == self_co:
            out["self"].append(p["name"])
            out["self_lines"][p.get("line") or "其他"] = out["self_lines"].get(p.get("line") or "其他", 0) + 1
        else:
            out["competitors"][p["company"]] = out["competitors"].get(p["company"], 0) + 1
    out["self"].sort()
    return out


def summary_line(imp: dict[str, Any]) -> str:
    """一行摘要（情報牆列表用）。"""
    parts = []
    if imp.get("scope"):
        comp = sum(imp["competitors"].values())
        parts.append(f"自家 {len(imp['self'])} 個商品（{imp['scope']}），競品 {comp} 個")
    if imp.get("untracked"):
        parts.append("、".join(imp["untracked"]) + "（未收錄）")
    if imp.get("company_level"):
        parts.append("、".join(imp["company_level"]))
    return "；".join(parts)
