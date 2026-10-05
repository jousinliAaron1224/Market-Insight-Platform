"""市場數據（供給面）：從 M3 的法定公開商品清單（核准／修正日期）算各家投資型商品的出手節奏。

資料限制（頁面上也會寫明）：
- 清單是各公司「現行」商品的名稱、日期及文號，已停售的商品不在清單上，所以越早的年份越低估；
  看「近 12 個月」「近幾季」的比較比較可靠。
- 一個「系列」通常同時送審好幾個版本（新臺幣／外幣、年金／萬能、甲乙型），所以同時列「商品數」與「系列數」。
- 修正日期常是主管機關要求的全面逕修（同一個金管會令），會另外標出來，不算各家的策略調整。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

LINE_SUFFIX = re.compile(r"(變額年金保險|變額萬能壽險|變額壽險)$")


def family_of(company: str, name: str) -> str:
    """系列名稱：去掉公司名、幣別、險種字尾、(甲型)(乙型)(112) 之類的版本字樣。"""
    s = name.replace(company, "", 1)
    s = re.sub(r"[(（](甲型|乙型|丙型|\d{2,3})[)）]", "", s)
    s = LINE_SUFFIX.sub("", s)
    s = s.replace("外幣", "").replace("美元", "").replace("澳幣", "")
    return s.strip() or name


def quarter(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def supply(conn, self_company: str | None = None, quarters: int = 12) -> dict[str, Any]:
    prods = {(r["company"], r["name"]): dict(r) for r in conn.execute(
        "SELECT company, name, line, currency, status, filings FROM product_terms")}
    rows = []
    for r in conn.execute("""SELECT meta FROM raw_docs WHERE source_id LIKE 'company_%'
                             ORDER BY version"""):
        m = json.loads(r["meta"])
        key = (m.get("company"), m.get("title"))
        if key not in prods:
            continue
        p = prods[key]
        p["first_date"], p["latest_date"] = m.get("first_date"), m.get("latest_date")   # 最新版本覆蓋
        rows.append(key)
    items = [p for p in prods.values() if p.get("first_date")]
    if not items:
        return {"as_of": None, "companies": [], "conclusions": [], "quarters": [], "recent": [], "revision_waves": []}

    dates = [d for p in items for d in (p["first_date"], p["latest_date"]) if d]
    as_of = max(dates)
    y_ago = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    companies = sorted({p["company"] for p in items}, key=lambda c: (c != self_company, c))

    def foreign(p):
        return (p.get("currency") or "TWD") != "TWD"

    per: dict[str, dict[str, Any]] = {}
    for co in companies:
        ps = [p for p in items if p["company"] == co]
        on = [p for p in ps if p.get("status") in (None, "on_sale")]
        new12 = [p for p in ps if p["first_date"] >= y_ago]
        rev12 = [p for p in ps if p["latest_date"] and p["latest_date"] != p["first_date"] and p["latest_date"] >= y_ago]
        per[co] = {
            "company": co, "on_sale": len(on), "families": len({family_of(co, p["name"]) for p in on}),
            "lines": dict(Counter(p.get("line") or "其他" for p in on)),
            "foreign": sum(foreign(p) for p in on),
            "new_12m": len(new12), "new_families_12m": len({family_of(co, p["name"]) for p in new12}),
            "new_12m_foreign": sum(foreign(p) for p in new12),
            "new_12m_lines": dict(Counter(p.get("line") or "其他" for p in new12)),
            "revised_12m": len(rev12),
            "latest_approval": max(p["first_date"] for p in ps),
        }

    # 每季新核准（商品數），最近 N 季
    q_end = quarter(as_of)
    y, qn = int(q_end[:4]), int(q_end[-1])
    qs = []
    for _ in range(quarters):
        qs.append(f"{y}Q{qn}")
        qn -= 1
        if qn == 0:
            y, qn = y - 1, 4
    qs.reverse()
    series = {q: {co: 0 for co in companies} for q in qs}
    for p in items:
        q = quarter(p["first_date"])
        if q in series:
            series[q][p["company"]] += 1

    # 修正潮：依月份統計修正件數，並找同一天的文號（條款前言）看是不是同一個主管機關令
    waves = defaultdict(lambda: {"count": 0, "docs": Counter(), "companies": Counter()})
    for p in items:
        if not p["latest_date"] or p["latest_date"] == p["first_date"]:
            continue
        month = p["latest_date"][:7]
        w = waves[month]
        w["count"] += 1
        w["companies"][p["company"]] += 1
        for f in json.loads(p.get("filings") or "[]"):
            if f.get("date") == p["latest_date"] and "金管" in (f.get("doc_no") or ""):
                w["docs"][f["doc_no"]] += 1
    top_waves = sorted(waves.items(), key=lambda kv: -kv[1]["count"])[:6]
    revision_waves = [{"month": m, "count": w["count"], "companies": dict(w["companies"]),
                       "top_doc": (w["docs"].most_common(1)[0] if w["docs"] else None)} for m, w in top_waves]

    recent = sorted((p for p in items if p["first_date"] >= (date.fromisoformat(as_of) - timedelta(days=180)).isoformat()),
                    key=lambda p: p["first_date"], reverse=True)
    recent = [{"company": p["company"], "name": p["name"], "line": p.get("line"), "currency": p.get("currency"),
               "first_date": p["first_date"]} for p in recent]

    return {"as_of": as_of, "from_12m": y_ago, "self_company": self_company, "companies": [per[c] for c in companies],
            "quarters": [{"quarter": q, **series[q]} for q in qs], "recent": recent,
            "revision_waves": revision_waves,
            "conclusions": conclusions(per, self_company, revision_waves, as_of, y_ago)}


def _pct(a: int, b: int) -> str:
    return f"{round(a * 100 / b)}%" if b else "—"


def conclusions(per: dict[str, dict], self_co: str | None, waves: list[dict], as_of: str, y_ago: str) -> list[str]:
    out = []
    rank = sorted(per.values(), key=lambda x: -x["new_12m"])
    total_new = sum(x["new_12m"] for x in rank)
    out.append(f"近 12 個月（{y_ago}～{as_of}）五家共新核准 {total_new} 個投資型商品："
               + "、".join(f"{x['company']} {x['new_12m']}（{x['new_families_12m']} 個系列）" for x in rank) + "。")
    if self_co in per:
        me = per[self_co]
        pos = [x["company"] for x in rank].index(self_co) + 1
        out.append(f"{self_co}新品數排第 {pos}／{len(rank)}；銷售中 {me['on_sale']} 個商品、{me['families']} 個系列，"
                   f"外幣商品占 {_pct(me['foreign'], me['on_sale'])}。")
    tf = sum(x["new_12m_foreign"] for x in rank)
    lines = Counter()
    for x in rank:
        lines.update(x["new_12m_lines"])
    if total_new:
        top_line, n = lines.most_common(1)[0]
        out.append(f"近 12 個月新品中外幣商品占 {_pct(tf, total_new)}，險種以{top_line}最多（{_pct(n, total_new)}）。")
    if waves and waves[0]["count"] >= 10:
        w = waves[0]
        cause = f"，其中 {w['top_doc'][1]} 件依同一主管機關文號（{w['top_doc'][0]}）逕修，屬全面性法規修正" if w["top_doc"] else ""
        out.append(f"條款修正最集中在 {w['month']}（{w['count']} 件，分布於 {len(w['companies'])} 家公司）{cause}。")
    return out
