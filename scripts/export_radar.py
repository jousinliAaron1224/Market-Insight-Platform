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
import math
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
from parsers.gist import gist                         # noqa: E402

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


def source_text(store, raw_doc_id: int, source_id: str) -> str:
    """一行主旨要用的原文：新聞 RSS 用暫存導言（30 天後清除，D16）；金管會裁罰、草案用 RSS 內文；新聞稿、法規用存下來的網頁。"""
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        if source_id == "news_rss":
            r = c.execute("SELECT lead_text FROM news_leads WHERE raw_doc_id=? AND expires_at > ?",
                          (raw_doc_id, datetime.now(timezone.utc).isoformat())).fetchone()
            return r[0] if r else ""
        r = c.execute("SELECT raw_path FROM raw_docs WHERE id=?", (raw_doc_id,)).fetchone()
    finally:
        c.close()
    if not r:
        return ""
    path = Path(store.db_path).parent / r[0]
    if not path.exists():
        return ""
    data = path.read_bytes()
    if source_id in ("fsc_penalty", "fsc_draft"):
        return json.loads(data).get("description", "")
    if source_id == "fsc_press":
        from adapters.fsc_press import parse_detail
        return parse_detail(data, "").get("body_text", "")
    if source_id == "tii_law_rss":
        return data.decode("cp950", errors="replace").split("本系統由")[0]
    return ""


LINE_NAME = {"inv": "投資型", "par": "分紅", "mort": "房貸壽險", "usd": "美元利變・年金"}


def apply_ai(news, regs, store):
    """有 AI 判讀（scripts/ai_insights.py）的情報與法規，用 AI 的影響說明、優先度、負責部門與建議行動取代規則判讀。"""
    from scripts.ai_insights import load
    ai = load(store.db_path)
    for kind, rows in (("news", news), ("regs", regs)):
        for r in rows:
            a = ai.get(r.pop("_rid", None))
            if not a:
                continue
            lines = a["lines"] or r["lines"]
            r.update(why=a["why"], imp=a["priority"], lines=lines,
                     ai={"model": a["model"], "at": a["at"][:10], "owner": a["owner"], "priority": a["priority"], "actions": a["actions"]})
            if kind == "news":
                r["impact"] = {"lines": lines, "dir": a["impact_dir"], "text": a["impact_text"]}
            else:
                r.update(analysis=a["impact_text"], design=a["actions"] or r["design"], affected=[LINE_NAME[x] for x in lines] or r["affected"])


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
        try:
            g = gist(d.get("source_id") or "", source_text(store, l["raw_doc_id"], d.get("source_id") or ""))
        except Exception:   # 原文解析不到就不顯示主旨，不影響匯出
            g = ""
        if g and g.rstrip("。…") in title:   # 主旨和標題一樣就不重複
            g = ""
        base = {"_rid": l["raw_doc_id"], "d": d["date"], "t": title, "src": d.get("source"), "real": True, "auto": True, "url": d.get("url"), "gist": g or None,
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
    apply_ai(news, regs, store)
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
            "series": series, "companies": companies,
            # 全市場每月中位數（所有有公告的商品，不限固定樣本）：和美債殖利率對照用
            "market_trend": [{"m": m, "med": _q(sorted(p["v"][m] for p in by.values() if m in p["v"]), .5),
                              "n": sum(1 for p in by.values() if m in p["v"])} for m in months if any(m in p["v"] for p in by.values())]}


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


# ---------- 商品規格變動：同一商品的條款前後兩版，欄位有差異就發一則情報 ----------
SPEC_FIELDS = [   # (extract_features 的欄位, 顯示名稱, 格式)
    ("premium_charge_pct", "前置費用", "pct"), ("surrender_max_pct", "解約費用上限", "pct"), ("distribution", "提解／撥回機制", "bool"),
    ("stop_profit", "停利機制", "bool"), ("bonus", "加值給付", "bool"), ("partial_withdrawal", "部分提領", "bool"),
    ("min_death_guarantee", "最低身故保證", "bool"), ("death_type", "身故給付型態", "list"), ("annuity_guarantee_years", "年金保證期間（年）", "list"),
    ("accumulation_min_years", "年金累積期間下限（年）", "num"), ("discretionary", "全權委託帳戶", "bool"), ("basic_amount_change", "基本保額變更", "bool")]
MAX_FIELD_CHANGES = 5   # 一次改超過這麼多欄位，通常是兩版內容不一樣（抓錯檔、換了解析規則），不是商品改版 → 不發情報


def _spec_val(v, kind):
    if v is None:
        return "未載明"
    return {"pct": lambda x: f"{x:g}%", "bool": lambda x: "有" if x else "無", "list": lambda x: "、".join(map(str, x)), "num": lambda x: f"{x:g}"}[kind](v)


def spec_changes(store):
    """clause_docs 同一商品的相鄰兩版 → 欄位差異。回傳 (changes, skipped)。

    只比兩版「確定是同一商品」的條款：兩版的條款前言（seq 0）都要出現商品名稱。
    2026-10-03 台灣人壽第一次抓取時 40 張商品抓到的是「全權委託帳戶批註條款」，不是商品條款本身，硬比會得到假的改版。"""
    from web.product_lab import extract_features
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        docs = [dict(r) for r in c.execute("""SELECT d.raw_doc_id, d.item_key, d.company, d.product_name, d.version, d.parser, r.fetched_at, r.url
                                              FROM clause_docs d JOIN raw_docs r ON r.id = d.raw_doc_id
                                              WHERE d.product_name IS NOT NULL ORDER BY d.item_key, d.version""")]
        terms = {(r["company"], r["name"]): dict(r) for r in c.execute("SELECT * FROM product_terms")}

        def load(rid):
            return [dict(r) for r in c.execute("SELECT seq, kind, article_no, title, text FROM clause_articles WHERE raw_doc_id=? ORDER BY seq", (rid,))]

        def feats(d, arts):
            t = terms.get((d["company"], d["product_name"])) or {}
            # 兩版都只用條文內容抽欄位（不帶 product_terms 的給付項目、幣別），這樣比的是同一套規則
            return extract_features({"name": d["product_name"], "line": t.get("line"), "currency": "", "payment_modes": "[]", "coverage": "[]", "riders": None}, arts)

        by: dict[str, list] = {}
        for d in docs:
            by.setdefault(d["item_key"], []).append(d)
        out, skipped = [], []
        for key, vs in by.items():
            for a, b in zip(vs, vs[1:]):
                aa, ba = load(a["raw_doc_id"]), load(b["raw_doc_id"])
                name = re.sub(r"\s+", "", b["product_name"])
                pre = lambda arts: re.sub(r"\s+", "", next((x["text"] for x in arts if x["seq"] == 0), ""))   # noqa: E731
                if not aa or not ba or name not in pre(aa) or name not in pre(ba) or a["parser"] != b["parser"]:
                    skipped.append((b["company"], b["product_name"], "兩版不是同一份商品條款"))
                    continue
                fa, fb = feats(a, aa), feats(b, ba)
                ch = [(lab, _spec_val(fa.get(k), kind), _spec_val(fb.get(k), kind), k) for k, lab, kind in SPEC_FIELDS if fa.get(k) != fb.get(k)]
                if not ch:
                    continue
                if len(ch) > MAX_FIELD_CHANGES:
                    skipped.append((b["company"], b["product_name"], f"一次 {len(ch)} 個欄位不同，疑似抽取結果不一致"))
                    continue
                out.append({"company": b["company"], "name": b["product_name"], "from": a["fetched_at"][:10], "to": b["fetched_at"][:10],
                            "changes": ch, "url": (terms.get((b["company"], b["product_name"])) or {}).get("clause_url") or b["url"]})
        return out, skipped
    finally:
        c.close()


def spec_news(changes, pids, today: date, days: int = 90):
    """規格變動 → 情報：每張商品每次改版一則。費用類欄位有變重要性 4，其他 3；自家商品算「公司策略」。"""
    since = (today - timedelta(days=days)).isoformat()
    names = {v: k for k, v in CO.items()}
    out = []
    for x in sorted(changes, key=lambda x: x["to"], reverse=True):
        if x["to"] < since:
            continue
        co = CO.get(x["company"])
        who = "法巴人壽" if co == "cardif" else x["company"]
        short = x["name"].replace(x["company"], "", 1) or x["name"]
        fee = any(k in ("premium_charge_pct", "surrender_max_pct") for *_, k in x["changes"])
        head = "、".join(f"{lab} {a} → {b}" for lab, a, b, _ in x["changes"][:2]) + (f"等 {len(x['changes'])} 項" if len(x["changes"]) > 2 else "")
        n = _news(x["to"], f"{who}{short} 條款調整：{head}", f"{who}官網商品條款（兩個版本比對）", "公司策略" if co == "cardif" else "競品新商品", [co] if co else [], ["inv"],
                  4 if fee and co != "cardif" else 3,
                  "自家商品條款調整" if co == "cardif" else ("競品調整費用條件，會改變銀行理專比較投資型商品的成本" if fee else "競品調整商品機制，需確認是否影響和法巴商品的比較"),
                  [f"來源：{who}官網商品條款，系統在 {x['from']} 與 {x['to']} 各抓一版比對", *[f"{lab}：{a} → {b}" for lab, a, b, _ in x["changes"]]],
                  "到商品資料庫看這張商品的最新條款，和法巴同險種商品並排比較。", f"company_{co}_products" if co else "company", x["url"])
        pid = pids.get(x["name"])
        n["prods"], n["prodCount"] = ([pid] if pid else []), (1 if pid else 0)
        out.append(n)
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


def macro(store, rates_data=None):
    """總體經濟指標（adapters/macro.py）：每個序列取最新一版，日資料轉成月底值，算最新值與一個月、一年的變化。"""
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        rows = c.execute("""SELECT item_key, url, meta FROM raw_docs WHERE source_id='macro' AND id IN
                            (SELECT MAX(id) FROM raw_docs WHERE source_id='macro' GROUP BY item_key)""").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    if not rows:
        return None
    out, conc = {"series": {}}, []

    def at_or_before(pts, d):
        v = [p for p in pts if p[0] <= d]
        return v[-1] if v else None

    raw = {}   # 序列 id → (名稱, 單位, 來源, 頁面, 點, transform)；一個檔案拆成多個序列的（CPI、薪資）展開
    for key, url, meta in rows:
        m = json.loads(meta)
        sid, pts = m["series"], m["points"]
        if m["kind"] == "cbc":
            steps = [p for p in pts if p[0] >= "2015-01-01"]
            out["cbc"] = {"name": m["title"], "source": m.get("source_name"), "page": m.get("page"), "url": url,
                          "steps": steps, "latest": pts[-1], "prev": pts[-2] if len(pts) > 1 else None}
            continue
        if m.get("multi"):
            for k, v in m["multi"].items():
                raw[k] = (v["name"], v.get("unit"), m.get("source_name"), m.get("page"), v["points"], v.get("transform"))
        else:
            raw[sid] = (m["title"], m.get("unit"), m.get("source_name"), m.get("page"), pts, m.get("transform"))

    def series(name, unit, src, page, pts, extra=None):
        last = pts[-1]
        d = date.fromisoformat(last[0])
        monthly = {}
        for p in pts:   # 月底值（每月最後一個交易日）
            monthly[p[0][:7]] = p[1]
        ser = {"name": name, "unit": unit, "source": src, "page": page, "latest": last,
               "m1": at_or_before(pts, (d - timedelta(days=30)).isoformat()), "y1": at_or_before(pts, (d - timedelta(days=365)).isoformat()),
               "monthly": [[k, v] for k, v in sorted(monthly.items())][-60:]}
        return dict(ser, **(extra or {}))

    def yoy(pts):   # 月資料 → 年增率（%）
        by = {p[0][:7]: p[1] for p in pts}
        out_ = []
        for mth, v in sorted(by.items()):
            prev = f"{int(mth[:4]) - 1}{mth[4:]}"
            if prev in by and by[prev]:
                out_.append([mth + "-01", round((v / by[prev] - 1) * 100, 2)])
        return out_

    for sid, (name, unit, src, page, pts, tf) in raw.items():
        if not pts:
            continue
        if tf == "yoy":
            y = yoy(pts)
            if y:
                out["series"][sid] = series(name + ("" if name.endswith("年增率") else " 年增率" if name[-1].isascii() else "年增率"), "%", src, page, y,
                                            {"level": pts[-1], "level_unit": unit})
            continue
        ser = series(name, unit, src, page, pts)
        if sid == "fed":   # 最近一次調整
            chg = next(((pts[i - 1], pts[i]) for i in range(len(pts) - 1, 0, -1) if pts[i][1] != pts[i - 1][1]), None)
            ser["last_change"] = {"date": chg[1][0], "from": chg[0][1], "to": chg[1][1]} if chg else None
        out["series"][sid] = ser

    FRED = ("FRED（聖路易聯準銀行）", "https://fred.stlouisfed.org/")
    pts_of = lambda sid: raw[sid][4] if sid in raw else []   # noqa: E731
    # 新台幣交叉匯率：同一天的美元兌新台幣 × 各幣別兌美元
    if "usdtwd" in raw:
        twd = dict(map(tuple, pts_of("usdtwd")))
        for sid, base, name, f in [("twd_jpy", "jpyusd", "新台幣兌日圓（每 100 日圓）", lambda t, x: t / x * 100),
                                   ("twd_eur", "usdeur", "新台幣兌歐元", lambda t, x: t * x), ("twd_cny", "cnyusd", "新台幣兌人民幣", lambda t, x: t / x),
                                   ("twd_aud", "usdaud", "新台幣兌澳幣", lambda t, x: t * x), ("twd_gbp", "usdgbp", "新台幣兌英鎊", lambda t, x: t * x)]:
            cross = [[d_, round(f(twd[d_], v), 4)] for d_, v in pts_of(base) if d_ in twd and v]
            if cross:
                out["series"][sid] = series(name, "元", FRED[0], FRED[1] + "series/" + {"jpyusd": "DEXJPUS", "usdeur": "DEXUSEU", "cnyusd": "DEXCHUS", "usdaud": "DEXUSAL", "usdgbp": "DEXUSUK"}[base], cross)
    # 美國公債殖利率曲線：1–30 年，現在與一年前
    tenors = [(1, "ust1"), (2, "ust2"), (3, "ust3"), (5, "ust5"), (7, "ust7"), (10, "ust10"), (20, "ust20"), (30, "ust30")]
    if all(t[1] in raw for t in tenors):
        ref = date.fromisoformat(pts_of("ust10")[-1][0])
        out["curve"] = {"date": ref.isoformat(), "points": [{"y": y, "now": pts_of(s_)[-1][1],
                        "m1": (at_or_before(pts_of(s_), (ref - timedelta(days=30)).isoformat()) or [None, None])[1],
                        "y1": (at_or_before(pts_of(s_), (ref - timedelta(days=365)).isoformat()) or [None, None])[1]} for y, s_ in tenors]}
    # 債券波動率：美債 10 年期殖利率每日變動（基點）近 21 個交易日的標準差，年化（MOVE 指數不是公開資料，改用實現波動率）
    if "ust10" in raw:
        p10 = pts_of("ust10")
        chg = [(p10[i][0], (p10[i][1] - p10[i - 1][1]) * 100) for i in range(1, len(p10))]
        vol = []
        for i in range(21, len(chg) + 1):
            w = [x for _, x in chg[i - 21:i]]
            mu = sum(w) / len(w)
            vol.append([chg[i - 1][0], round((sum((x - mu) ** 2 for x in w) / (len(w) - 1)) ** 0.5 * 252 ** 0.5, 1)])
        if vol:
            out["series"]["bond_vol"] = series("美債 10 年期殖利率波動率（近 21 個交易日，年化）", "bp", "FRED（聖路易聯準銀行）DGS10，自行計算", FRED[1] + "series/DGS10", vol)
    # 主要資產：近一年報酬、今年以來報酬、年化波動、最大跌幅
    assets = []
    for sid in ("sp500", "nasdaq", "ig_tr", "hy_tr", "wti", "dxy"):
        pts = pts_of(sid)
        if len(pts) < 30:
            continue
        last = pts[-1]
        d = date.fromisoformat(last[0])
        start = at_or_before(pts, (d - timedelta(days=365)).isoformat())
        ystart = at_or_before(pts, f"{d.year - 1}-12-31")
        win = [p for p in pts if p[0] >= (start or pts[0])[0]]
        rets = [math.log(win[i][1] / win[i - 1][1]) for i in range(1, len(win)) if win[i - 1][1] > 0 and win[i][1] > 0]
        mu = sum(rets) / len(rets)
        peak, mdd = win[0][1], 0.0
        for _, v in win:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        monthly = {}
        for p in win:
            monthly[p[0][:7]] = p[1]
        assets.append({"id": sid, "name": raw[sid][0], "latest": last, "ret_1y": round((last[1] / start[1] - 1) * 100, 2) if start else None,
                       "ret_ytd": round((last[1] / ystart[1] - 1) * 100, 2) if ystart else None,
                       "vol_1y": round((sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * 252 ** 0.5 * 100, 2),
                       "mdd_1y": round(mdd * 100, 2), "path": [[k, round(v / win[0][1] * 100, 2)] for k, v in sorted(monthly.items())],
                       "page": FRED[1] + "series/" + {"sp500": "SP500", "nasdaq": "NASDAQCOM", "ig_tr": "BAMLCC0A0CMTRIV", "hy_tr": "BAMLHYH0A0HYM2TRIV", "wti": "DCOILWTICO", "dxy": "DTWEXBGS"}[sid]})
    if assets:
        out["assets"] = assets
    S = out["series"]
    pp = lambda x: f"{x:+.2f}".replace("+", "升 ").replace("-", "降 ")   # noqa: E731
    if "ust10" in S and S["ust10"]["m1"]:
        u = S["ust10"]
        conc.append(f"美國 10 年期公債殖利率 {u['latest'][1]:.2f}%（{u['latest'][0]}），一個月{pp(u['latest'][1] - u['m1'][1])} 個百分點"
                    + (f"、一年{pp(u['latest'][1] - u['y1'][1])} 個百分點。" if u["y1"] else "。"))
    if "fed" in S and S["fed"].get("last_change"):
        f, lc = S["fed"], S["fed"]["last_change"]
        conc.append(f"聯準會利率目標上限 {f['latest'][1]:.2f}%，最近一次在 {lc['date']} 由 {lc['from']:.2f}% {'調升' if lc['to'] > lc['from'] else '調降'}。")
    if rates_data and rates_data.get("market_trend") and "ust10" in S:
        mt = rates_data["market_trend"]
        ust = dict(S["ust10"]["monthly"])
        sp = [{"m": x["m"], "declared": x["med"], "ust10": ust.get(x["m"]), "spread": round(x["med"] - ust[x["m"]], 2) if x["m"] in ust else None} for x in mt]
        out["spread"] = sp
        ok = [x for x in sp if x["spread"] is not None]
        if len(ok) >= 2:
            a, b = ok[0], ok[-1]
            conc.append(f"美元利變宣告利率市場中位數 {b['declared']:.2f}%，比美債 10 年期 {'低' if b['spread'] < 0 else '高'} {abs(b['spread']):.2f} 個百分點"
                        f"（{a['m']} 時差 {a['spread']:+.2f}）；美債上升時宣告利率通常落後調整。")
    if "cbc" in out:
        cb = out["cbc"]
        conc.append(f"台灣央行重貼現率 {cb['latest'][1]:.3f}%，自 {cb['latest'][0]} 起未調整。")
    if "usdtwd" in S and S["usdtwd"]["y1"]:
        x = S["usdtwd"]
        chg = (x["latest"][1] / x["y1"][1] - 1) * 100
        conc.append(f"美元兌新台幣 {x['latest'][1]:.2f}（{x['latest'][0]}），一年前 {x['y1'][1]:.2f}，新台幣{'貶值' if chg > 0 else '升值'} {abs(chg):.1f}%。")
    out["conclusions"] = conc
    return out


def _open_data_latest(store, name):
    """open_data 某個資料集最新一版的原始檔 → (bytes, 抓取時間)；沒有就回 (None, None)。"""
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        r = c.execute("""SELECT raw_path, fetched_at FROM raw_docs WHERE source_id='open_data' AND json_extract(meta, '$.dataset')=?
                         ORDER BY id DESC LIMIT 1""", (name,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    finally:
        c.close()
    if not r:
        return None, None
    path = Path(store.db_path).parent / r[0]
    return (path.read_bytes(), r[1]) if path.exists() else (None, None)


def _age(a):
    """年齡區間的顯示文字：'30-40' → '30–40 歲'、'60以上' → '60 歲以上'。"""
    return a.replace("以上", " 歲以上") if "以上" in a else a.replace("-", "–") + " 歲"


def audience(store):
    """目標客群：聯徵中心房貸借款人（房貸壽險）、國發會人口年齡結構（年金、退休客群）。全部是公開統計，沒有個人資料。"""
    from parsers.open_data import parse_jcic_loan_age, parse_jcic_mortgage_age, parse_jcic_mortgage_gender, parse_ndc_population
    out, conc = {}, []
    src = lambda name, ds, fetched: {"name": name, "url": f"https://data.gov.tw/dataset/{ds}", "fetched_at": fetched}  # noqa: E731

    # 1. 新承做房貸借款人的年齡分布（每月）
    raw, at = _open_data_latest(store, "jcic_mortgage_age")
    rows = parse_jcic_mortgage_age(raw) if raw else []
    if rows:
        months = sorted({r["m"] for r in rows})
        last = months[-1]
        ages = [a for a in dict.fromkeys(r["age"] for r in rows)]
        by = {(r["m"], r["age"]): r for r in rows}
        cur = [by[(last, a)] for a in ages if (last, a) in by]
        tot = sum(r["n"] for r in cur) or 1

        def roll(ms):   # 12 個月合計的年齡占比（比單月穩定）
            n = {a: sum(by[(m, a)]["n"] for m in ms if (m, a) in by) for a in ages}
            t = sum(n.values()) or 1
            return {a: round(n[a] / t * 100, 1) for a in ages}
        r12, p12 = roll(months[-12:]), roll(months[-24:-12]) if len(months) >= 24 else None
        out["mortgage_age"] = {
            "latest": last, "total": tot,
            "groups": [{"age": r["age"], "n": r["n"], "share": round(r["n"] / tot * 100, 1), "avg_loan": round(r["amt"] / r["n"] * 100, 0) if r["n"] else None,
                        "rate": r["rate"], "share12": r12[r["age"]], "share12_prev": p12[r["age"]] if p12 else None} for r in cur],
            "monthly": [{"m": m, **{a: by[(m, a)]["n"] for a in ages if (m, a) in by}} for m in months[-36:]],
            "ages": ages, "source": src("金融聯合徵信中心（房貸借款人各年齡層新增授信）", 21946, at)}
        g = out["mortgage_age"]["groups"]
        core = [x for x in g if x["age"] in ("30-40", "40-50")]
        old = next((x for x in g if x["age"].startswith("60")), None)
        avg_all = round(sum(r["amt"] for r in cur) / tot * 100)
        conc.append(f"{last} 新承做房貸借款人 {tot:,} 人，30–50 歲占 {sum(x['share'] for x in core):.1f}%；平均每人新增房貸 {avg_all:,} 萬元"
                    + (f"（{_age(max(g, key=lambda x: x['avg_loan'] or 0)['age'])}最高，{max(x['avg_loan'] or 0 for x in g):,.0f} 萬元）。" if g else "。"))
        if old and old["share12_prev"] is not None:
            conc.append(f"近 12 個月 60 歲以上的新房貸借款人占 {old['share12']}%（前 12 個月 {old['share12_prev']}%）；房貸壽險的投保年齡上限與核保要能涵蓋這群人。")

    # 2. 房貸條件（每季，男女合計）
    raw, at = _open_data_latest(store, "jcic_mortgage_gender")
    rows = parse_jcic_mortgage_gender(raw) if raw else []
    if rows:
        qs = sorted({r["q"] for r in rows})[-20:]
        agg = []
        for q in qs:
            rs = [r for r in rows if r["q"] == q and r["n"]]
            n = sum(r["n"] for r in rs)
            w = lambda k: round(sum(r[k] * r["n"] for r in rs if r[k] is not None) / n, 2)  # noqa: E731
            f = sum(r["n"] for r in rs if r["sex"] == "女")
            y, m = q.split("-")
            agg.append({"q": f"{y}Q{(int(m) - 1) // 3 + 1}", "n": int(n), "female": round(f / n * 100, 1), "limit": round(w("limit") / 10),
                        "value": round(w("value") / 10), "ltv": w("ltv"), "rate": w("rate"), "area": w("area")})
        out["mortgage_terms"] = {"quarters": agg, "source": src("金融聯合徵信中心（購置住宅貸款統計分析表－性別，流量）", 113748, at)}
        b, a = agg[-1], (agg[-5] if len(agg) >= 5 else agg[0])
        conc.append(f"{b['q']} 新承做房貸平均授信額度 {b['limit']:,} 萬元（一年前 {a['limit']:,} 萬元），核貸成數 {b['ltv']:.1f}%、利率 {b['rate']:.2f}%；女性借款人占 {b['female']}%。")

    # 3. 房貸存量借款人的年齡（每年）
    raw, at = _open_data_latest(store, "jcic_loan_age")
    rows = [r for r in (parse_jcic_loan_age(raw) if raw else []) if r["type"] == "房貸" and r["age"] != "其他"]
    if rows:
        y = max(r["y"] for r in rows)
        ages = list(dict.fromkeys(r["age"] for r in rows if r["y"] == y))
        g = [{"age": a, "n": int(sum(r["n"] or 0 for r in rows if r["y"] == y and r["age"] == a)),
              "bal": round(sum(r["bal"] or 0 for r in rows if r["y"] == y and r["age"] == a) / 100)} for a in ages]   # 億元
        t = sum(x["n"] for x in g) or 1
        for x in g:
            x["share"] = round(x["n"] / t * 100, 1)
        out["mortgage_stock"] = {"year": y, "total": t, "groups": g, "source": src("金融聯合徵信中心（不同年齡及貸款類別授信）", 151726, at)}
        o = next((x for x in g if x["age"].startswith("65")), None)
        if o:
            conc.append(f"{y} 年底有房貸的人約 {t / 10000:,.0f} 萬人，其中 65 歲以上 {o['n'] / 10000:,.1f} 萬人（{o['share']}%），房貸餘額 {o['bal']:,} 億元。")

    # 4. 人口年齡結構（每年，各縣市）
    raw, at = _open_data_latest(store, "ndc_population")
    rows = parse_ndc_population(raw) if raw else []
    if rows:
        y = max(r["y"] for r in rows)
        rs = [r for r in rows if r["y"] == y]
        nat = next((r for r in rs if r["area"] == "臺閩地區"), rs[0])
        cty = [r for r in rs if r["area"].endswith(("市", "縣"))]
        out["population"] = {"year": y, "national": nat, "areas": sorted(cty, key=lambda r: -r["p65"]),
                             "source": src("國家發展委員會（都市及區域發展統計彙編 1-1）", 6326, at)}
        top = sorted(cty, key=lambda r: -r["p65"])[:3]
        tops = "、".join(f"{r['area']} {r['p65']}%" for r in top)
        stage = "已是超高齡社會（20% 以上）" if nat["p65"] >= 20 else "接近超高齡社會門檻 20%"
        conc.append(f"{y} 年底 65 歲以上人口占 {nat['p65']}%，{stage}；0–14 歲只占 {nat['p0_14']}%。高齡比例最高：{tops}。")
    if not out:
        return None
    out["conclusions"] = conc
    return out


def sources(store):
    meta = store.meta()
    names = dict(meta.get("source_labels") or {})
    names.update({"open_data": "政府資料開放平臺（104113 保費收入、7191 公司指標、14539 壽險業績、聯徵中心房貸、國發會人口）",
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
    sc, sc_skipped = spec_changes(store)
    cn = spec_news(sc, pids, now.date())
    for i, n in enumerate(cn):
        n["id"] = f"N{600 + i}"
    if sc_skipped:
        print(f"規格變動：略過 {len(sc_skipped)} 組（兩版不是同一份條款或抽取結果不一致），例：{sc_skipped[0][0]}{sc_skipped[0][1]}（{sc_skipped[0][2]}）")
    news = sorted(news + sn + rn + pn + cn, key=lambda n: n["d"] or "", reverse=True)
    products += shelf_products(store, cfg, shelf_data, start=400 + len(products))   # 接在條款商品後面編號，不能重複
    dup = {p["id"] for p in products if sum(q["id"] == p["id"] for q in products) > 1}
    if dup:
        raise SystemExit(f"商品編號重複：{sorted(dup)[:5]}")
    mk = market(store, pids)
    mk["fx_share"] = fx_share(store)
    mk["macro"] = macro(store, rate_data)
    mk["audience"] = audience(store)
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
