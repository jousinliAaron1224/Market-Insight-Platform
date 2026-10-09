"""把 insurance-intel 的真實資料匯出給「商品情報雷達」網站（paris-hackathon-business-competition）。

    python scripts/export_radar.py --out ../../paris-hackathon-business-competition/real-data.js

輸出一支 real-data.js（window.REAL_DATA = {...}），欄位已轉成雷達網站 data.js 的格式：
  products  投資型商品（條款規則抽取，不是 AI 估計）；分紅、房貸壽險、美元利變（銀行上架清單＋宣告利率）
  news      金管會新聞稿、裁罰、新聞（規則判讀，不是 AI 摘要）
  regs      保發中心法規異動與函釋
  market    保費收入（保發中心 104113）、公司指標（保險局 7191）、商品供給（各公司條款）
  sources   各資料來源的筆數與最後抓取時間
雷達網站的 real-merge.js 會用這些資料取代對應的模擬資料。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config, resolve          # noqa: E402
from web.server import build_store                    # noqa: E402
from scripts.export_site import raw_url_map, relink   # noqa: E402

# 雷達網站的公司代碼
CO = {"法商法國巴黎人壽": "cardif", "國泰人壽": "cathay", "富邦人壽": "fubon", "凱基人壽": "kgi", "台灣人壽": "taiwanlife"}
# 標題裡出現哪些字就算提到那家公司（順序：長的先比）
CO_WORDS = [("法國巴黎人壽", "cardif"), ("法巴人壽", "cardif"), ("台灣人壽", "taiwanlife"), ("安聯人壽", "allianz"), ("保誠人壽", "pru"),
            ("安達", "chubb"), ("友邦人壽", "aia"), ("國泰人壽", "cathay"), ("富邦人壽", "fubon"), ("台新", "taishin"), ("凱基人壽", "kgi"),
            ("第一金人壽", "firstlife"), ("元大人壽", "yuanta"), ("南山人壽", "nanshan")]
MISSING = {"issue_age": "投保年齡", "exclusions": "除外責任", "riders": "附約", "rate_terms": "利率條款", "payment_modes": "繳費方式"}
IMP = {"high": 4, "medium": 3, "low": 2}
INSURANCE_TITLE = re.compile("保險|壽險|人壽|保單|保費|年金|投資型|宣告利率")


def pct(x):
    return None if x is None else (int(x) if float(x).is_integer() else round(float(x), 2))


def mentioned(text: str) -> list[str]:
    out = []
    for w, cid in CO_WORDS:
        if w in text and cid not in out:
            out.append(cid)
    return out


def product_rows(store, today: date):
    prods = store.products({})["items"]
    with store.conn() as c:
        feats = {(f["company"], f["name"]): f for f in store.lab_features(c)}
    rows, ids = [], {}
    for i, p in enumerate(sorted(prods, key=lambda p: (list(CO).index(p["company"]) if p["company"] in CO else 9, p["name"]))):
        if p["company"] not in CO:
            continue
        pid = f"P{400 + i}"
        ids[p["name"]] = pid
        det = store.product(p["company"], p["name"]) or {}
        f = (feats.get((p["company"], p["name"])) or {}).get("features") or {}
        first = det.get("first_date") or ""
        if p.get("status") != "on_sale":
            status = "停售"
        elif first and first >= (today - timedelta(days=90)).isoformat():
            status = "新上市"
        else:
            status = "銷售中"
        cur = (det.get("currency") or p.get("currency") or "—").replace("/", "／")
        front, surr = pct(f.get("premium_charge_pct")), pct(f.get("surrender_max_pct"))
        ag = f.get("annuity_guarantee_years")
        ag = "、".join(map(str, ag)) if isinstance(ag, list) else ag
        mech = [t for k, t in (("distribution", "提解／撥回"), ("bonus", "加值給付"), ("stop_profit", "停利機制")) if f.get(k)]
        pros = []
        if front == 0:
            pros.append("條款載明免收前置費用")
        if f.get("partial_withdrawal"):
            pros.append("可部分提領")
        pros += [f"有{m}" for m in mech]
        if f.get("min_death_guarantee"):
            pros.append("有最低身故保證")
        if ag:
            pros.append(f"年金保證期間 {ag} 年")
        ncur = len([x for x in cur.split("／") if x])
        if ncur > 2:
            pros.append(f"多幣別（{ncur} 種）")
        clause = det.get("clause") or {}
        miss = [MISSING.get(m, m) for m in clause.get("missing", [])]
        cons = []
        if surr:
            cons.append(f"解約費用最高 {surr}%")
        if front:
            cons.append(f"前置費用 {front}%")
        if miss:
            cons.append("條款未載明：" + "、".join(miss))
        if status == "停售":
            cons.append("已停售")
        filings = sorted(det.get("filings") or [], key=lambda x: x.get("date") or "")
        versions = [{"d": x["date"], "c": f"{x.get('kind') or '核准'}（{x.get('doc_no') or '文號未載'}）"} for x in filings if x.get("date")]
        if not versions and first:
            versions = [{"d": first, "c": "核准／備查"}]
        name = p["name"]
        rows.append({
            "id": pid, "co": CO[p["company"]], "name": name, "short": name.replace(p["company"], "", 1) or name,
            "line": "inv", "sub": p.get("line") or det.get("line"), "cur": cur, "launch": first[:7] or "—", "status": status,
            "channel": [], "min": "—", "minTwd": None, "age": "—", "pay": "／".join(det.get("payment_modes") or []) or "—", "term": "—",
            "coverage": "、".join(det.get("coverage") or []) or "—",
            "fees": {"front": "—" if front is None else ("0%（免收）" if front == 0 else f"{front}%"), "admin": "—",
                     "surrender": "—" if surr is None else ("無" if surr == 0 else f"最高 {surr}%")},
            "premCharge": front, "surrMax": surr, "feeIdx": None, "guarantee": None, "declared": None, "predetermined": None,
            "riders": f.get("riders") or det.get("riders") or "—", "funds": None, "dividend": "、".join(mech) or "—",
            "deathType": "；".join(f.get("death_type") or []) or "—",
            "minGuar": "有" if f.get("min_death_guarantee") else ("無" if f.get("min_death_guarantee") is False else "—"),
            "segment": "—", "pros": pros or ["條款未見特殊機制"], "cons": cons or ["—"], "conf": {}, "reviewed": False,
            "real": True, "auto": True, "src": det.get("clause_url") or (p.get("clause_url")), "versions": versions,
            "articles": p.get("articles") or clause.get("articles"), "missing": miss,
            "feat": {"front": front, "surr": surr, "dist": bool(f.get("distribution")), "stop": bool(f.get("stop_profit")),
                     "bonus": bool(f.get("bonus")), "minG": bool(f.get("min_death_guarantee")), "wd": bool(f.get("partial_withdrawal"))},
        })
    return rows, ids


def label_rows(store, pids):
    labels = [x for x in store.labels({"hidden": "all", "limit": "2000"})["items"] if not x.get("hidden")]
    latest = {}   # 同一則公告改版（doc_revised）會有新舊兩筆分類，只留最新版
    for x in labels:
        k = (x.get("source_id"), x.get("url") or x["raw_doc_id"])
        if k not in latest or x["raw_doc_id"] > latest[k]["raw_doc_id"]:
            latest[k] = x
    labels = list(latest.values())
    news, regs = [], []
    for l in sorted(labels, key=lambda x: (x["date"] or "", x["raw_doc_id"]), reverse=True):
        d = store.impact_detail(l["raw_doc_id"]) or l
        pi = d.get("product_impact") or {}
        extra = d.get("extra") or {}
        reason = next((r.split(":", 1)[1].strip() for r in d.get("reasons") or [] if r.startswith(l["impact"] + ":")), "")
        scope = pi.get("scope") or ""
        lines = ["inv"] if (pi.get("self_count") or pi.get("competitor_count") or "投資型" in scope) else []
        prods = [pids[n] for n in pi.get("self_products") or [] if n in pids]
        n_prods, prods = len(prods), prods[:12]
        text = pi.get("summary") or ""
        if pi.get("self_count"):
            text = f"可能影響法巴 {pi['self_count']} 張、競品 {pi.get('competitor_count', 0)} 張投資型商品（{scope or '投資型商品'}）。"
        elif not text:
            text = "規則判讀沒有對應到特定商品，需人工判讀對法巴的影響。"
        why = reason or pi.get("summary") or ("分類：" + "、".join(d.get("categories")) if d.get("categories") else "規則判讀為一般動態")
        sum_ = [f"來源：{d.get('source')}" + (f"・{extra['unit']}" if extra.get("unit") else "") + (f"・{extra['data_type']}" if extra.get("data_type") else "")]
        if extra.get("fine_twd"):
            sum_.append(f"受處分：{extra.get('respondent', '—')}，罰鍰新臺幣 {extra['fine_twd'] / 10000:,.0f} 萬元" + (f"（{extra['doc_no']}）" if extra.get("doc_no") else ""))
        if pi.get("articles"):
            sum_.append("可能涉及的條款：" + "、".join(pi["articles"][:6]))
        if pi.get("reasons"):
            sum_.append("判讀依據：" + "、".join(pi["reasons"][:4]))
        title = d["title"]
        co = mentioned(title + (extra.get("respondent") or ""))
        base = {"d": d["date"], "t": title, "src": d.get("source"), "real": True, "auto": True, "url": d.get("url"),
                "lines": lines, "imp": IMP.get(d.get("impact"), 2), "why": why, "prods": prods, "prodCount": n_prods, "srcId": d.get("source_id")}
        if d.get("source_id") == "tii_law_rss":
            topic = (pi.get("company_level") or [None])[0] or ("投資型商品規範" if lines else "其他法規")
            design = [f"檢視條款「{a}」" for a in (pi.get("articles") or [])[:4]] + [r for r in (pi.get("reasons") or [])[:2]]
            regs.append(dict(base, topic=topic, status=extra.get("data_type") or "已發布", analysis=f"{reason}。{text}" if reason else text,
                             design=design or ["請法遵與商品開發人工判讀是否需調整商品"], affected=["投資型"] if lines else ["公司層級"]))
        elif d.get("source_id") == "fsc_draft":   # 法規草案預告：還沒定案，商品設計可以先準備
            period = (f"陳述意見至 {extra['comment_end']}" if extra.get("comment_end")
                      else f"刊登公報翌日起 {extra['comment_days']} 日內陳述意見" if extra.get("comment_days") else "陳述意見期限見原文")
            design = ["草案尚未定案：先評估對商品設計與送審時程的影響，必要時於預告期間陳述意見"]
            design += [f"檢視條款「{a}」" for a in (pi.get("articles") or [])[:3]]
            regs.append(dict(base, topic="法規草案預告", status="草案預告",
                             analysis=f"{extra.get('undertake') or '金管會'}預告，{period}。{text}",
                             design=design, affected=["投資型"] if lines else ["公司層級"]))
        elif d.get("source_id") == "news_rss" and not INSURANCE_TITLE.search(title):
            continue   # 新聞 RSS 是以「金管會」等關鍵字收進來的，標題沒提到保險的（例如虛擬資產、銀行）不放進情報
        elif d.get("impact") != "low":   # 情報動態只放中、高影響（低影響多為產險宣導、人事）
            cats = d.get("categories") or []
            if d.get("source_id") == "fsc_penalty" or "裁罰" in title or "法規" in cats:
                cat = "法規監理"
            elif re.search("銷售情形|統計|業績", title):
                cat = "市場數據"
            elif "利率" in cats:
                cat = "宣告利率"
            elif "人事" in cats:
                cat = "人事組織"
            elif "通路" in cats:
                cat = "通路動態"
            else:
                cat = "競品新商品" if co and re.search("新商品|保單|商品|上市|推出|開賣", title) else "公司策略"   # 只提到公司名稱（例如得獎）不算新商品
            news.append(dict(base, cat=cat, co=co, ch="—", cur="外幣" if "外幣" in scope + title else "—", seg="—", ev=None, en=None,
                             sum=sum_, impact={"lines": lines, "dir": "neu", "text": text}))
    for i, n in enumerate(news):
        n["id"] = f"N{500 + i}"
    for i, g in enumerate(regs):
        g["id"] = f"G{100 + i}"
    return news, regs


def market(store, pids):
    sup, dem, com = store.market_supply(), store.market_demand(), store.market_companies()
    m = lambda d: {CO.get(k, k): v for k, v in d.items()}   # noqa: E731
    supply = {
        "as_of": sup.get("as_of"), "from_12m": sup.get("from_12m"), "conclusions": sup.get("conclusions", []),
        "companies": [dict(c, co=CO.get(c["company"])) for c in sup.get("companies", [])],
        "quarters": [m(q) for q in sup.get("quarters", [])],
        "recent": [dict(r, co=CO.get(r["company"]), id=pids.get(r["name"])) for r in sup.get("recent", [])],
    }
    demand = {k: dem.get(k) for k in ("as_of", "lines", "conclusions", "ytd_label", "ytd_last_label", "ytd", "monthly", "annual", "investment_ref")}
    demand["source"] = {k: (dem.get("source") or {}).get(k) for k in ("name", "url", "fetched_at")}
    comp = {"period": com.get("period"), "n_companies": com.get("n_companies"), "conclusions": com.get("conclusions", []),
            "indicators": [dict(i, values=m(i.get("values") or {})) for i in com.get("indicators", [])],
            "growth_trend": [m(g) for g in com.get("growth_trend", [])],
            "source": {k: (com.get("source") or {}).get(k) for k in ("name", "url", "fetched_at")}}
    return {"supply": supply, "demand": demand, "companies": comp}


RATE_MONTHS = 7   # 雷達網站的利率圖與快報固定讀 7 個月（v[6] 是最新月、v[5] 是上個月）


def _month_add(m: str, k: int) -> str:
    y, mo = map(int, m.split("-"))
    n = y * 12 + (mo - 1) + k
    return f"{n // 12}-{n % 12 + 1:02d}"


def _q(xs: list[float], q: float) -> float:
    """分位數（線性內插）；xs 已排序。"""
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo = int(k)
    return round(xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo), 4)


def rates(store, cfg):
    """宣告利率（declared_rates）→ 雷達網站的利率頁：看各公司的「分布」，不挑代表商品。

    - dist：最新月份每家公司的分布（最低、四分位、中位數、最高、張數）
    - moves：最新月份與上個月相比，同一張商品調升／調降／不變的張數（宣告利率很少動，「有沒有調」本身就是訊號）
    - movers：最新月份調整幅度最大的商品
    - series：每家公司 7 個月的中位數趨勢；只用 7 個月都有公告的商品（固定樣本），避免新舊商品替換造成假的漲跌
    """
    src = next((s for s in cfg.get("sources", []) if s["id"] == "declared_rates"), {})
    names = {c["id"]: c["name"] for c in src.get("companies", [])}
    pages = {c["id"]: c["page_url"] for c in src.get("companies", [])}
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM declared_rates")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    if not rows:
        return None
    latest = max(r["month"] for r in rows)
    prev = _month_add(latest, -1)
    months = [_month_add(latest, k) for k in range(-(RATE_MONTHS - 1), 1)]
    by = {}
    for r in sorted(rows, key=lambda r: r["month"]):   # 名稱以最新月份為準（凱基的歷史 API 會回傳併購前的「中國人壽…」舊名）
        p = by.setdefault((r["company"], r["product_code"]), {"v": {}})
        p["name"] = r["product_name"]
        p["v"][r["month"]] = r["rate_pct"]
    dist, moves, movers, series, companies = [], {}, [], [], []
    for co in dict.fromkeys(r["company"] for r in rows):
        prods = [p for (cc, _), p in by.items() if cc == co]
        now = sorted(p["v"][latest] for p in prods if latest in p["v"])
        if now:
            dist.append({"co": co, "name": names.get(co, co), "n": len(now), "min": now[0], "p25": _q(now, .25),
                         "med": _q(now, .5), "p75": _q(now, .75), "max": now[-1]})
        up = down = same = 0
        for p in prods:
            if latest in p["v"] and prev in p["v"]:
                d = round(p["v"][latest] - p["v"][prev], 4)
                up, down, same = up + (d > 0), down + (d < 0), same + (d == 0)
                if d:
                    movers.append({"co": co, "name": p["name"].replace(names.get(co, ""), "", 1) or p["name"],
                                   "from": p["v"][prev], "to": p["v"][latest]})
        moves[co] = {"up": up, "down": down, "same": same}
        panel = [p for p in prods if all(m in p["v"] for m in months)]
        if panel:
            series.append({"co": co, "label": f"{names.get(co, co)}（中位數，{len(panel)} 張）", "n": len(panel),
                           "v": [_q(sorted(p["v"][m] for p in panel), .5) for m in months]})
        companies.append({"co": co, "name": names.get(co, co), "products": len(prods), "source": pages.get(co)})
    # 近 7 個月每月調升／調降張數（同一張商品與前一個月比），以及最近一次有調整的月份與明細
    hist = []
    for m in months[1:]:
        pm = _month_add(m, -1)
        row = {"m": m, "up": 0, "down": 0, "by": {}}
        for (co, _), p in by.items():
            if m in p["v"] and pm in p["v"] and p["v"][m] != p["v"][pm]:
                k = "up" if p["v"][m] > p["v"][pm] else "down"
                row[k] += 1
                b = row["by"].setdefault(co, {"up": 0, "down": 0, "steps": {}})
                b[k] += 1
                step = f"{p['v'][m] - p['v'][pm]:+.2f}"   # 調幅分布，例如 {"+0.05": 150}
                b["steps"][step] = b["steps"].get(step, 0) + 1
        hist.append(row)
    last_change = next((h["m"] for h in reversed(hist) if h["up"] or h["down"]), None)
    if last_change and last_change != latest:
        lm, pm = last_change, _month_add(last_change, -1)
        movers = [{"co": co, "name": p["name"].replace(names.get(co, ""), "", 1) or p["name"], "from": p["v"][pm], "to": p["v"][lm]}
                  for (co, _), p in by.items() if lm in p["v"] and pm in p["v"] and p["v"][lm] != p["v"][pm]]
    movers.sort(key=lambda m: -abs(m["to"] - m["from"]))
    allnow = sorted(p["v"][latest] for p in by.values() if latest in p["v"])
    return {"months": months, "latest": latest, "prev": prev, "market": {"n": len(allnow), "med": _q(allnow, .5)},
            "dist": dist, "moves": moves, "history": hist, "last_change": last_change, "movers": movers[:8],
            "series": series, "companies": companies}


SHELF_LINE = {"investment": "inv", "participating": "par", "mortgage_term": "mort", "usd_interest": "usd", "annuity": "usd"}


def shelf(store, cfg, today: date):
    """銀行通路上架（bank_shelf）：各銀行目前上架的商品，以及最近 30 天的上架／下架（不含首次建立的基準）。"""
    banks = {b["id"]: b["name"] for s in cfg.get("sources", []) if s["id"] == "bank_shelf" for b in s.get("banks", [])}
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM bank_shelf ORDER BY bank_id, insurer, product")]
    except sqlite3.OperationalError:   # 舊資料庫還沒有這張表
        rows = []
    finally:
        c.close()
    local = lambda ts: datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=8))).date().isoformat() if ts else None  # noqa: E731
    out, changes = [], []
    since = (today - timedelta(days=30)).isoformat()
    for bid in dict.fromkeys(r["bank_id"] for r in rows):
        items = []
        for r in (x for x in rows if x["bank_id"] == bid):
            co = (mentioned(r["insurer"]) or [None])[0]
            it = {"co": co, "insurer": r["insurer"], "product": r["product"], "line": SHELF_LINE.get(r["line"]),
                  "category": r["category"] or None, "currency": r["currency"] or None,
                  "first_seen": local(r["first_seen"]), "removed": local(r["removed_at"]), "baseline": bool(r["baseline"])}
            items.append(it)
            if not it["baseline"] and it["first_seen"] >= since and not it["removed"]:
                changes.append(dict(it, bank=bid, kind="上架", d=it["first_seen"]))
            if it["removed"] and it["removed"] >= since:
                changes.append(dict(it, bank=bid, kind="下架", d=it["removed"]))
        live = [i for i in items if not i["removed"]]
        by_co = {}
        for i in live:
            by_co[i["co"] or i["insurer"]] = by_co.get(i["co"] or i["insurer"], 0) + 1
        out.append({"id": bid, "name": banks.get(bid, bid), "count": len(live), "by_company": by_co, "items": items})
    return {"as_of": today.isoformat(), "banks": out}, changes


SHELF_CO = {"新光人壽": "skl", "全球人壽": "transglobe", "合作金庫人壽": "tcblife", "遠雄人壽": "farglory"}   # 雷達網站原本沒有的公司


def _norm(name: str, insurer: str = "") -> str:
    """商品名稱比對用：去掉公司名、括號、破折號與空白（銀行與公司官網的寫法常常只差全形／半形）。"""
    s = name.replace(insurer, "", 1) if insurer else name
    s = re.sub(r"^(法國巴黎|國泰|富邦|凱基|中國|台灣|南山|保誠|安達|友邦|新光|全球|遠雄|第一金|合作金庫|安聯|元大)人壽(保險)?", "", s)
    return re.sub(r"[\s()（）\[\]【】\-－–—_．.・]", "", s)


def shelf_products(store, cfg, shelf_data, start: int = 600):
    """銀行上架清單 → 分紅、房貸壽險、美元利變／年金商品（取代雷達網站的示範商品）。

    銀行清單只有公司、商品名稱、險種與幣別；費用、保費門檻、年齡等規格要看條款，這裡一律留「—」，不估計。
    美元利變／年金商品如果在宣告利率資料裡找得到同名商品，就帶入最新月份的宣告利率。
    """
    banks = {b["id"]: b["name"] for b in shelf_data["banks"]}
    rate_src = {c["id"]: c["page_url"] for s in cfg.get("sources", []) if s["id"] == "declared_rates" for c in s.get("companies", [])}
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        dr = c.execute("SELECT company, product_name, month, rate_pct FROM declared_rates ORDER BY month").fetchall()
    except sqlite3.OperationalError:
        dr = []
    finally:
        c.close()
    latest, hist = {}, {}   # (公司, 正規化名稱) → (月份, 利率)／{月份: 利率}；同名多張時以最新月份為準
    for co, name, m, v in dr:
        latest[(co, _norm(name))] = (m, v)
        hist.setdefault((co, _norm(name)), {})[m] = v
    months = sorted({m for _, _, m, _ in dr})[-RATE_MONTHS:]
    by = {}
    for b in shelf_data["banks"]:
        for it in b["items"]:
            if it["removed"] or it["line"] not in ("par", "mort", "usd"):
                continue
            co = it["co"] or SHELF_CO.get(it["insurer"])
            if not co:
                continue
            p = by.setdefault((co, _norm(it["product"], it["insurer"])),
                              {"co": co, "insurer": it["insurer"], "name": it["product"], "line": it["line"], "cur": set(), "banks": [], "cat": set()})
            if b["id"] not in p["banks"]:
                p["banks"].append(b["id"])
            cur = it["currency"] or ""
            p["cur"].add("USD" if re.search("美元|美金", cur) else "TWD" if re.search("臺幣|台幣", cur) else "")
            if it["category"]:
                p["cat"].add(it["category"])
    order = ["cardif", "allianz", "cathay", "fubon", "kgi", "taiwanlife", "nanshan"]
    rows = []
    for i, p in enumerate(sorted(by.values(), key=lambda p: (order.index(p["co"]) if p["co"] in order else 9, p["co"], p["line"], p["name"]))):
        cur = next((x for x in p["cur"] if x), "") or ("USD" if re.search("美元|外幣", p["name"]) else "TWD")
        bank_names = [banks.get(x, x) for x in p["banks"]]
        key = (p["co"], _norm(p["name"], p["insurer"]))
        rate = latest.get(key)
        rate_hist = [[m, hist[key][m]] for m in months if m in hist.get(key, {})]
        pros = [f"{len(bank_names)} 家銀行上架（{'、'.join(bank_names)}）"]
        if rate:
            pros.append(f"{rate[0]} 宣告利率 {rate[1]}%")
        if "高資產" in p["name"]:
            pros.append("高資產客戶限定")
        rows.append({
            "id": f"P{start + i}", "co": p["co"], "name": p["name"], "short": p["name"], "line": p["line"], "sub": "、".join(sorted(p["cat"])) or None,
            "cur": cur, "launch": "—", "status": "銷售中", "channel": ["銀行"], "banks": bank_names,
            "min": "—", "minTwd": None, "age": "—", "pay": "—", "term": "終身" if "終身" in p["name"] else "—", "coverage": "—",
            "fees": {"front": "—", "admin": "—", "surrender": "—"}, "premCharge": None, "surrMax": None, "feeIdx": None, "guarantee": None,
            "declared": rate[1] if rate else None, "declaredMonth": rate[0] if rate else None, "rateSrc": rate_src.get(p["co"]) if rate else None,
            "rateHist": rate_hist or None,   # 最近 7 個月的宣告利率 [[月份, 利率], …]
            "predetermined": None, "riders": "—", "funds": None, "dividend": "—", "segment": "高資產客戶" if "高資產" in p["name"] else "—",
            "pros": pros, "cons": ["銀行上架清單只有商品名稱與險種，費用、保費門檻、投保年齡請看條款"],
            "conf": {}, "reviewed": True, "real": True, "auto": False, "shelf": True, "src": None, "versions": [], "missing": [],
        })
    return rows


def shelf_news(changes, banks):
    """上架／下架 → 情報動態（通路動態）。重要度：法巴自家或主力險種 4，其他 3。"""
    names = {b["id"]: b["name"] for b in banks}
    news = []
    for ch in sorted(changes, key=lambda x: x["d"], reverse=True):
        who = ch["insurer"]
        main = ch["co"] == "cardif" or ch["line"] in ("inv", "par", "mort", "usd")
        t = f"{names.get(ch['bank'], ch['bank'])}{ch['kind']}{who}「{ch['product']}」"
        text = ("法巴商品在此銀行的上架狀態有變，請通路確認" if ch["co"] == "cardif"
                else f"競品在法巴的銀行通路{'新增' if ch['kind'] == '上架' else '減少'}商品，請通路評估櫃位與話術")
        news.append({"d": ch["d"], "t": t, "src": "銀行官網上架清單", "real": True, "auto": True, "url": None,
                     "lines": [ch["line"]] if ch["line"] else [], "imp": 4 if main else 3,
                     "why": f"銀行上架清單比對：{ch['kind']}", "prods": [], "prodCount": 0, "srcId": "bank_shelf",
                     "cat": "通路動態", "co": [ch["co"]] if ch["co"] else [], "ch": "銀行", "cur": ch["currency"] or "—", "seg": "—",
                     "ev": None, "en": None, "sum": [f"來源：{names.get(ch['bank'], ch['bank'])}官網保險商品列表（每日比對）",
                                                     f"銀行分類：{ch['category'] or '—'}"],
                     "impact": {"lines": [ch["line"]] if ch["line"] else [], "dir": "neu", "text": text}})
    return news


def _news(d, t, src, cat, co, lines, imp, why, sums, text, src_id, url=None, cur="—", ch="—"):
    return {"d": d, "t": t, "src": src, "real": True, "auto": True, "url": url, "lines": lines, "imp": imp, "why": why,
            "prods": [], "prodCount": 0, "srcId": src_id, "cat": cat, "co": co, "ch": ch, "cur": cur, "seg": "—",
            "ev": None, "en": None, "sum": sums, "impact": {"lines": lines, "dir": "neu", "text": text}}


def rate_news(r):
    """宣告利率（declared_rates）→ 情報：每家公司每個月有調整就一則；最新月份全部持平也發一則（「沒調」本身就是訊號）。"""
    if not r:
        return []
    names = {c["co"]: c["name"] for c in r["companies"]}
    pages = {c["co"]: c["source"] for c in r["companies"]}
    out = []
    for h in r["history"]:
        mo = int(h["m"][5:])
        for co, b in h["by"].items():
            steps = "、".join(f"{k} 個百分點 {v} 張" for k, v in sorted(b["steps"].items(), key=lambda x: -x[1])[:3])
            act = "、".join(x for x in (f"調升 {b['up']} 張" if b["up"] else "", f"調降 {b['down']} 張" if b["down"] else "") if x)
            down = b["down"] > b["up"]
            out.append(_news(
                f"{h['m']}-01", f"{names.get(co, co)} {mo} 月美元利變商品宣告利率{act}", f"{names.get(co, co)}官網宣告利率", "宣告利率",
                [co], ["usd"], 4 if b["up"] + b["down"] >= 10 else 3,
                f"宣告利率{'下調' if down else '上調'}會改變銀行理專比較美元利變商品時的排序",
                [f"來源：{names.get(co, co)}官網每月宣告利率公告（每月比對同一張商品）", f"調整幅度：{steps}"],
                f"{names.get(co, co)}{'調降' if down else '調升'}宣告利率；法巴官網未公告美元利變宣告利率，請精算與商品開發評估是否需要對應。",
                "declared_rates", pages.get(co), "USD", "全通路"))
    if r["latest"] != r["last_change"]:
        mo = int(r["latest"][5:])
        n = sum(sum(v.values()) for v in r["moves"].values())
        out.append(_news(
            f"{r['latest']}-01", f"{mo} 月美元利變宣告利率：{len(r['moves'])} 家公司 {n} 張商品全數持平", "各公司官網宣告利率", "宣告利率",
            list(r["moves"]), ["usd"], 3, "宣告利率連續持平，代表利率競賽暫歇",
            [f"來源：{'、'.join(names.values())}官網每月宣告利率公告", f"市場中位數 {r['market']['med']}%（{r['market']['n']} 張）",
             f"最近一次有調整的月份：{r['last_change'] or '近 7 個月沒有'}"],
            "各家利率持平，競爭焦點會轉到保費門檻、通路與附加服務。", "declared_rates", None, "USD", "全通路"))
    return out


def product_news(products, today: date, days: int = 90):
    """條款資料的新核准商品 → 情報：同一家公司同一天核准的合併成一則。"""
    since = (today - timedelta(days=days)).isoformat()
    groups = {}
    for p in products:
        first = (p.get("versions") or [{}])[0].get("d")
        if p.get("status") == "停售" or not first or first < since:
            continue
        groups.setdefault((p["co"], first), []).append(p)
    names = {v: k for k, v in CO.items()}
    out = []
    for (co, d), ps in sorted(groups.items(), key=lambda x: x[0][1], reverse=True):
        who = "法巴人壽" if co == "cardif" else names.get(co, co)
        subs = sorted({p.get("sub") or "投資型" for p in ps})
        n = _news(d, f"{who}新核准 {len(ps)} 張投資型商品：{'、'.join(p['short'] for p in ps[:3])}{'等' if len(ps) > 3 else ''}",
                  f"{who}官網法定公開商品清單", "公司策略" if co == "cardif" else "競品新商品", [co], ["inv"], 3 if co == "cardif" else 4,
                  "自家新商品上架" if co == "cardif" else "競品投資型新商品，可能和法巴爭取同一批銀行客戶",
                  [f"來源：{who}官網商品條款與核准文號", f"險種：{'、'.join(subs)}", "商品：" + "、".join(p["short"] for p in ps[:6])],
                  "請到商品資料庫看條款特色（前置費用、解約費用、撥回機制），和法巴同險種商品比較。", f"company_{co}_products",
                  ps[0].get("src"))
        n["prods"], n["prodCount"] = [p["id"] for p in ps[:12]], len(ps)
        out.append(n)
    return out


def fx_share(store):
    """外幣保單占新契約保費比率：金管會每月「外幣保險商品銷售情形」新聞稿的附件表格（fsc_press 的 attachment_tables）。"""
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        row = c.execute("""SELECT url, meta FROM raw_docs WHERE source_id='fsc_press' AND meta LIKE '%占整體新契約保費收入比率%'
                           AND json_extract(meta, '$.tables') IS NOT NULL ORDER BY json_extract(meta, '$.published_at') DESC, id DESC LIMIT 1""").fetchone()
    finally:
        c.close()
    if not row:
        return None
    m = json.loads(row[1])
    t = next(t for t in m["tables"] if "比率" in t["name"])
    grid = t["tables"][0]
    head = next(r for r in grid if r[0].startswith("年"))
    vals = next(r for r in grid if r[0].startswith("占比"))
    pts = []
    for h, v in zip(head[1:], vals[1:]):
        mm = re.match(r"(\d{2,3})/(\d{1,2})", h)
        if mm and v.rstrip("%"):
            y, mo = int(mm.group(1)) + 1911, int(mm.group(2))
            pts.append({"p": f"{y}" if mo == 12 else f"{y} 年 1–{mo} 月", "y": y, "fx": float(v.rstrip("%"))})
    return {"title": t["name"], "press": m.get("title"), "date": (m.get("published_at") or "")[:10], "url": row[0],
            "pdf": t["url"], "points": pts}


def sources(store):
    meta = store.meta()
    names = dict(meta.get("source_labels") or {})
    names.update({"open_data": "政府資料開放平臺（104113 保費收入、7191 公司指標、14539 壽險業績）",
                  "bank_shelf": "銀行官網保險商品列表（兆豐、華南、永豐）",
                  "declared_rates": "各公司官網宣告利率（國泰、保誠、凱基、台灣人壽、南山）"})
    kind = lambda s: "公司網站（商品條款）" if s.startswith("company_") else {"news_rss": "新聞 RSS", "open_data": "開放資料 API", "bank_shelf": "銀行官網", "declared_rates": "公司網站（宣告利率）"}.get(s, "官方網站")  # noqa: E731
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    out = []
    for sid, n, last in c.execute("SELECT source_id, COUNT(*), MAX(fetched_at) FROM raw_docs GROUP BY source_id ORDER BY source_id"):
        t = datetime.fromisoformat(last).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if last else "—"
        out.append({"id": sid, "name": names.get(sid, sid), "type": kind(sid), "freq": "手動執行（原型）", "status": "ok", "last": t, "items": n})
    c.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="real-data.js 的輸出路徑")
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    cfg = load_config()
    store = build_store(cfg, a.db)
    links = raw_url_map(store.db_path)
    now = datetime.now(timezone(timedelta(hours=8)))
    products, pids = product_rows(store, now.date())
    news, regs = label_rows(store, pids)
    shelf_data, shelf_changes = shelf(store, cfg, now.date())
    sn = shelf_news(shelf_changes, shelf_data["banks"])
    for i, n in enumerate(sn):
        n["id"] = f"N{900 + i}"
    rate_data = rates(store, cfg)
    rn = rate_news(rate_data)
    for i, n in enumerate(rn):
        n["id"] = f"N{700 + i}"
    pn = product_news(products, now.date())
    for i, n in enumerate(pn):
        n["id"] = f"N{800 + i}"
    news = sorted(news + sn + rn + pn, key=lambda n: n["d"] or "", reverse=True)
    products += shelf_products(store, cfg, shelf_data, start=400 + len(products))   # 接在條款商品後面編號，不能重複
    dup = {p["id"] for p in products if sum(q["id"] == p["id"] for q in products) > 1}
    if dup:
        raise SystemExit(f"商品編號重複：{sorted(dup)[:5]}")
    mk = market(store, pids)
    mk["fx_share"] = fx_share(store)
    data = {"meta": {"snapshot_at": now.strftime("%Y-%m-%d %H:%M"), "today": now.date().isoformat(), "self_company": store.self_company,
                     "counts": {"products": len(products), "news": len(news), "regs": len(regs),
                                "shelf": sum(b["count"] for b in shelf_data["banks"])}},
            "products": products, "news": news, "regs": regs, "market": mk, "shelf": shelf_data,
            "rates": rate_data,
            "sources": sources(store)}
    body = json.dumps(relink(data, links), ensure_ascii=False, separators=(",", ":"))
    out = resolve(a.out)
    tmp = out.with_name(out.name + ".tmp")   # 先寫暫存檔再換名，避免寫到一半的檔案被網站讀到
    tmp.write_text("/* 自動產生：insurance-intel scripts/export_radar.py，請勿手動修改。"
                   f"資料快照 {data['meta']['snapshot_at']} */\nwindow.REAL_DATA = {body};\n", encoding="utf-8")
    tmp.replace(out)
    print(f"匯出完成：{out}（商品 {len(products)}、情報 {len(news)}、法規 {len(regs)}，{out.stat().st_size / 1e3:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
