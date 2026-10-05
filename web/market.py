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


# ====================================================================== 需求面
def _latest_open_data(conn, dataset: str) -> dict[str, Any] | None:
    best = None
    for r in conn.execute("""SELECT raw_path, fetched_at, meta, version FROM raw_docs
                             WHERE source_id='open_data' ORDER BY fetched_at, version"""):
        m = json.loads(r["meta"])
        if m.get("dataset") == dataset:
            best = {"raw_path": r["raw_path"], "fetched_at": r["fetched_at"], "meta": m}
    return best


def _yi(v: float) -> float:
    """百萬元 → 億元（一位小數）"""
    return round(v / 100, 1)


def _g(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 1) if b else None


def demand(conn, read_raw) -> dict[str, Any]:
    """全市場保費收入依險種（TII I10）＋壽險公會 14539 的投資型參考值。read_raw(raw_path) → bytes。"""
    from parsers.open_data import LINES, parse_lia_14539, parse_tii_i10

    out: dict[str, Any] = {"as_of": None, "lines": LINES, "conclusions": [], "errors": []}
    src = _latest_open_data(conn, "ib_premium_monthly")
    if src:
        try:
            t = parse_tii_i10(read_raw(src["raw_path"]))
        except Exception as e:
            out["errors"].append(f"保費收入統計表解析失敗：{e}")
            t = None
        if t:
            out.update(_demand_from_i10(t))
            out["source"] = {"name": "保險事業發展中心「人身保險業保費收入」（政府資料開放平臺 104113）",
                             "url": src["meta"].get("final_url"), "raw": f"/{src['raw_path']}",
                             "fetched_at": src["fetched_at"]}
    ref = _latest_open_data(conn, "lia_performance")
    if ref:
        try:
            rows = parse_lia_14539(read_raw(ref["raw_path"]))
            out["investment_ref"] = _investment_ref(rows)
            out["investment_ref"]["raw"] = f"/{ref['raw_path']}"
        except Exception as e:
            out["errors"].append(f"壽險業績統計解析失敗：{e}")
    return out


def _demand_from_i10(t: dict[str, Any]) -> dict[str, Any]:
    from parsers.open_data import LINES

    monthly = t["monthly"]
    last = monthly[-1]
    y, m = int(last["month"][:4]), int(last["month"][5:])
    by_month = {x["month"]: x for x in monthly}

    def ytd(year):
        rows = [by_month.get(f"{year}-{i:02d}") for i in range(1, m + 1)]
        if any(r is None for r in rows):
            return None
        return {"total": sum(r["total"] for r in rows),
                "lines": {ln: sum(r["lines"][ln] for r in rows) for ln in LINES}}

    cur, prev = ytd(y), ytd(y - 1)
    ytd_rows = []
    if cur and prev:
        for ln in LINES:
            a, b = cur["lines"][ln], prev["lines"][ln]
            ytd_rows.append({"line": ln, "this": _yi(a), "last": _yi(b), "growth": _g(a, b),
                             "share": round(a * 100 / cur["total"], 1), "share_last": round(b * 100 / prev["total"], 1)})
        ytd_rows.append({"line": "合計", "this": _yi(cur["total"]), "last": _yi(prev["total"]),
                         "growth": _g(cur["total"], prev["total"]), "share": 100.0, "share_last": 100.0})

    recent = [{"month": x["month"], "total": _yi(x["total"]), "yoy": x["yoy"],
               **{ln: _yi(x["lines"][ln]) for ln in LINES}} for x in monthly[-24:]]
    annual = [{"year": a["year"], "total": _yi(a["total"]), "yoy": a["yoy"],
               **{ln: _yi(a["lines"][ln]) for ln in LINES},
               "annuity_share": round(a["lines"]["個人年金"] * 100 / a["total"], 1)} for a in t["annual"]]
    res = {"as_of": last["month"], "announced": last.get("announced"), "ytd_label": f"{y} 年 1–{m} 月",
           "ytd_last_label": f"{y - 1} 年 1–{m} 月", "ytd": ytd_rows, "monthly": recent, "annual": annual}
    res["conclusions"] = demand_conclusions(res)
    return res


def demand_conclusions(d: dict[str, Any]) -> list[str]:
    out = []
    rows = {r["line"]: r for r in d.get("ytd", [])}
    if "合計" in rows:
        tot = rows["合計"]
        out.append(f"{d['ytd_label']}人身保險業保費收入 {tot['this']:,.0f} 億元，比去年同期 {tot['growth']:+.1f}%"
                   f"（含續期保費，不只新契約）。")
        lines = [r for k, r in rows.items() if k != "合計" and r["growth"] is not None]
        fast = max(lines, key=lambda r: r["growth"])
        out.append(f"成長最快的是{fast['line']}：{fast['this']:,.0f} 億元、{fast['growth']:+.1f}%，"
                   f"占比由 {fast['share_last']}% 升到 {fast['share']}%；"
                   + "、".join(f"{r['line']} {r['growth']:+.1f}%" for r in lines if r is not fast) + "。")
    ann = (d.get("annual") or [])[-10:]
    if "個人年金" in rows and len(ann) >= 5:
        now = rows["個人年金"]["share"]
        avg = round(sum(a["annuity_share"] for a in ann) / len(ann), 1)
        higher = [a for a in d["annual"] if a["annuity_share"] >= now]
        last_hi = f"上一次全年這麼高是 {higher[-1]['year']} 年（{higher[-1]['annuity_share']}%）" if higher \
            else "是有資料以來最高"
        out.append(f"{d['ytd_label']}個人年金占比 {now}%，{'高於' if now > avg else '不高於'} {ann[0]['year']}–{ann[-1]['year']} 年全年平均 {avg}%；{last_hi}。")
    return out


def _investment_ref(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = {r["item"]: r for r in rows if r["item"].startswith("初年度")}
    inv, trad = first.get("初年度投資型"), first.get("初年度傳統型")
    res: dict[str, Any] = {"rows": rows, "announced": rows[0]["announced"] if rows else None}
    if inv and trad and inv["this"] and trad["this"]:
        share = inv["this"] * 100 / (inv["this"] + trad["this"])
        res["summary"] = (f"初年度保費投資型 {_yi(inv['this']):,.0f} 億元（{inv['growth']:+.1f}%），"
                          f"占初年度保費 {share:.1f}%")
    return res
