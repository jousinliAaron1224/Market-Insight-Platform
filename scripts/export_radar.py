"""把 insurance-intel 的真實資料匯出給「商品情報雷達」網站（paris-hackathon-business-competition）。

    python scripts/export_radar.py --out ../../paris-hackathon-business-competition/real-data.js

輸出一支 real-data.js（window.REAL_DATA = {...}），欄位已轉成雷達網站 data.js 的格式：
  products  投資型商品（條款規則抽取，不是 AI 估計）
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
                cat = "競品新商品" if co else "公司策略"
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


def sources(store):
    meta = store.meta()
    names = dict(meta.get("source_labels") or {})
    names.update({"open_data": "政府資料開放平臺（104113 保費收入、7191 公司指標、14539 壽險業績）"})
    kind = lambda s: "公司網站（商品條款）" if s.startswith("company_") else {"news_rss": "新聞 RSS", "open_data": "開放資料 API"}.get(s, "官方網站")  # noqa: E731
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
    store = build_store(load_config(), a.db)
    links = raw_url_map(store.db_path)
    now = datetime.now(timezone(timedelta(hours=8)))
    products, pids = product_rows(store, now.date())
    news, regs = label_rows(store, pids)
    data = {"meta": {"snapshot_at": now.strftime("%Y-%m-%d %H:%M"), "today": now.date().isoformat(), "self_company": store.self_company,
                     "counts": {"products": len(products), "news": len(news), "regs": len(regs)}},
            "products": products, "news": news, "regs": regs, "market": market(store, pids), "sources": sources(store)}
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
