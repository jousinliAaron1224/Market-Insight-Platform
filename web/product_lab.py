"""模組化商品開發工作台（D30）：商品草案、各模組的競品／法規／新聞／市場情境、檢查提示、規格書。

- 模組定義：config/product_modules.yaml（9 個模組；欄位、對應的競品欄位、條文關鍵字、法規、關鍵字、市場數據）
- 競品欄位：從已解析的條款（clause_articles，含附表）用規則擷取（FEATURES）；擷取不到就是 None，
  畫面上一律附原文頁碼連結，請以原文為準。
- 草案：存在另一個可寫的 SQLite（drafts.db，與唯讀的情報資料庫分開）。
- 檢查提示（CHECKS）：只提示「要確認」並附法規出處，不判斷合不合法。
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from core.config import resolve

MODULES_PATH = "config/product_modules.yaml"

CUR_LABEL = {"TWD": "新臺幣", "USD": "美元", "AUD": "澳幣", "CNY": "人民幣", "EUR": "歐元", "JPY": "日圓"}
PAY_MAP = {"躉繳": "躉繳", "定期繳": "定期繳", "分期繳": "定期繳", "年繳": "定期繳", "半年繳": "定期繳", "季繳": "定期繳",
           "月繳": "定期繳", "不定期繳": "不定期繳／彈性繳", "彈性繳": "不定期繳／彈性繳"}
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
PCT = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*%")      # 費用率合理範圍 0–99%
CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")
LAW_EXCLUDE = ["財產保險", "產物保險", "汽車", "機車", "住宅地震", "火災", "旅行不便", "登山", "巨災"]


def load_modules(path: str | Path | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else resolve(MODULES_PATH)
    return yaml.safe_load(p.read_text(encoding="utf-8"))["modules"]


def cn2int(s: str) -> int | None:
    """「十五」「二十」「5」→ 整數（只處理 1–99）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(ch not in CN_NUM and ch != "十" for ch in s):
        return None
    if "十" not in s:
        return CN_NUM.get(s)
    a, _, b = s.partition("十")
    return (CN_NUM.get(a, 1) if a else 1) * 10 + (CN_NUM.get(b, 0) if b else 0)


# ====================================================================== 競品欄位擷取
def _first_pct_window(text: str, key: str, stop: tuple[str, ...], before: int = 30, after: int = 200) -> tuple[float | None, int | None]:
    i = text.find(key)
    if i < 0:
        return None, None
    tail = text[i + len(key):i + len(key) + after]
    if re.match(r"[\s:：]*(?:\d\.\s*\S{0,4}\s*)?(無|免收|0%|不收取)", tail):
        return 0.0, i
    end = len(tail)
    for s in stop:
        j = tail.find(s)
        if 0 <= j < end:
            end = j
    win = text[max(0, i - before):i] + tail[:end]
    vals = [float(x) for x in PCT.findall(win)]
    return (max(vals) if vals else None), i


def extract_features(prod: dict[str, Any], arts: list[dict[str, Any]]) -> dict[str, Any]:
    """一個商品的條款條文（含附表）→ 統一欄位。prod 是 product_terms 的一列。"""
    titles = [a["title"] or "" for a in arts if a["kind"] == "article"]
    defs = next((a["text"] for a in arts if (a["title"] or "") == "名詞定義"), "")
    # 表格標題常被拆成「保 費 費 用」：只去掉中文字之間的空白（數字間的空白要留，否則「500 3.2%」會黏成 5003.2%）
    appendix = CJK_GAP.sub("", "\n".join(a["text"] for a in arts if a["kind"] == "appendix"))
    alltext = "\n".join(a["text"] for a in arts)
    coverage = json.loads(prod.get("coverage") or "[]")
    name, line = prod["name"], prod.get("line")
    f: dict[str, Any] = {"line": line}
    codes = [c for c in (prod.get("currency") or "").split("/") if c]
    f["currency"] = sorted({CUR_LABEL.get(c, "其他外幣") for c in codes}) or None
    f["payment_modes"] = sorted({PAY_MAP.get(p, p) for p in json.loads(prod.get("payment_modes") or "[]")}) or None
    types = []
    if line == "變額年金保險":       # 年金型的「甲型／乙型」多指年金給付方式，不是身故給付型態
        types.append("返還保單帳戶價值（年金型）")
    else:
        if "甲型" in name or "甲型" in defs:
            types.append("甲型（保額與帳戶價值取大）")
        if "乙型" in name or "乙型" in defs:
            types.append("乙型（保額加帳戶價值）")
    f["death_type"] = types or None
    f["min_death_guarantee"] = any("保證" in c for c in coverage) or any("保證" in t and "身故" in t for t in titles)
    f["disability"] = any(c.startswith("完全") for c in coverage)
    f["maturity_benefit"] = any("祝壽" in c for c in coverage)
    years: set[int] = set()
    for sent in re.split(r"[。\n]{1}(?=[一二三四五六七八九十]+、)|。", defs):
        if "保證期間" in sent and ("選擇" in sent or "為" in sent):
            years |= {y for y in (cn2int(x) for x in re.findall(r"([一二三四五六七八九十\d]+)年", sent)) if y and 5 <= y <= 40}
    for m in re.finditer(r"(\d+(?:、\d+)*(?:或\d+)?)年", appendix[appendix.find("保證期間"):][:600] if "保證期間" in appendix else ""):
        years |= {int(x) for x in re.findall(r"\d+", m.group(1)) if 5 <= int(x) <= 40}
    f["annuity_guarantee_years"] = [str(y) for y in sorted(years)] or None
    m = re.search(r"年金累積期間[^。]{0,80}?不得(?:低|少)於([一二三四五六七八九十\d]+)\s*年", defs + alltext)
    f["accumulation_min_years"] = cn2int(m.group(1)) if m else None
    f["discretionary"] = "全權委託" in alltext or "全委帳戶" in alltext
    f["distribution"] = "撥回" in appendix or "撥回" in name   # 示範條款都有「收益分配或撥回」條，看附表／標的批註才準
    f["stop_profit"] = any("停利" in t for t in titles)
    f["bonus"] = any(("加值" in t or "回饋金" in t) for t in titles) or "加值回饋金" in appendix
    f["premium_charge_pct"], _ = _first_pct_window(appendix, "保費費用", ("二、", "保單管理費", "保險相關費用"))
    key = "解約費用率" if "解約費用率" in appendix else "解約費用"
    f["surrender_max_pct"], _ = _first_pct_window(appendix, key, ("部分提領費用", "2.部分", "(2)", "五、"), before=0, after=900)
    f["partial_withdrawal"] = any("部分提領" in t for t in titles)
    f["basic_amount_change"] = any("基本保額" in t for t in titles)
    f["riders"] = prod.get("riders") or None
    return f


class FeatureCache:
    """每個資料庫算一次（條款不會在伺服器執行中改變；資料庫檔案變了就重算）。"""

    def __init__(self):
        self._key = None
        self._rows: list[dict[str, Any]] = []

    def get(self, conn, db_path: Path) -> list[dict[str, Any]]:
        key = (str(db_path), db_path.stat().st_mtime if db_path.exists() else 0)
        if key != self._key:
            self._rows = competitor_features(conn)
            self._key = key
        return self._rows


def competitor_features(conn) -> list[dict[str, Any]]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "product_terms" not in tables:
        return []
    prods = [dict(r) for r in conn.execute("SELECT * FROM product_terms WHERE clause_raw_doc_id IS NOT NULL")]
    ids = [p["clause_raw_doc_id"] for p in prods]
    arts: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    raw_paths: dict[int, str] = {}
    first: dict[tuple[str, str], tuple[str, str]] = {}
    if ids:
        q = ",".join("?" * len(ids))
        for r in conn.execute(f"""SELECT raw_doc_id, seq, kind, article_no, title, text, page_start
                                  FROM clause_articles WHERE raw_doc_id IN ({q}) ORDER BY raw_doc_id, seq""", ids):
            arts[r["raw_doc_id"]].append(dict(r))
        raw_paths = {r[0]: r[1] for r in conn.execute(f"SELECT id, raw_path FROM raw_docs WHERE id IN ({q})", ids)}
    for r in conn.execute("SELECT meta FROM raw_docs WHERE source_id LIKE 'company_%' ORDER BY version"):
        m = json.loads(r["meta"])
        if m.get("company") and m.get("title"):
            first[(m["company"], m["title"])] = (m.get("first_date") or "", m.get("latest_date") or "")
    out = []
    for p in prods:
        a = arts.get(p["clause_raw_doc_id"], [])
        fd, ld = first.get((p["company"], p["name"]), ("", ""))
        out.append({"company": p["company"], "name": p["name"], "line": p["line"], "currency_code": p["currency"],
                    "status": p["status"], "raw_doc_id": p["clause_raw_doc_id"],
                    "pdf": f"/{raw_paths[p['clause_raw_doc_id']]}" if p["clause_raw_doc_id"] in raw_paths else None,
                    "first_date": fd, "latest_date": ld, "features": extract_features(p, a)})
    return out


def summarize(items: list[dict[str, Any]], feature: str, self_company: str | None) -> dict[str, Any] | None:
    vals = [(it, it["features"].get(feature)) for it in items]
    vals = [(it, v) for it, v in vals if v not in (None, [], "")]
    if not vals:
        return {"kind": "none", "n": 0}
    sample = vals[0][1]
    if isinstance(sample, bool) or isinstance(sample, (list, str)):
        counts: dict[str, list[int]] = {}
        for it, v in vals:
            keys = v if isinstance(v, list) else (["是"] if v is True else ["否"] if v is False else [str(v)])
            for k in keys:
                c = counts.setdefault(k, [0, 0])
                c[0] += 1
                c[1] += it["company"] == self_company
        rows = sorted(([k, n, s] for k, (n, s) in counts.items()), key=lambda x: -x[1])
        return {"kind": "counts", "n": len(vals), "self_n": sum(it["company"] == self_company for it, _ in vals),
                "counts": rows}
    nums = [float(v) for _, v in vals]
    by_co: dict[str, list[float]] = {}
    for it, v in vals:
        by_co.setdefault(it["company"], []).append(float(v))
    return {"kind": "range", "n": len(nums), "min": min(nums), "median": statistics.median(nums), "max": max(nums),
            "by_company": {c: {"min": min(v), "max": max(v), "n": len(v)} for c, v in by_co.items()}}


# ====================================================================== 檢查提示
def _f(draft: dict, key: str, default=None):
    v = (draft.get("fields") or {}).get(key)
    return default if v in (None, "", []) else v


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def checks(draft: dict[str, Any], modules: list[dict[str, Any]], feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    add = lambda module, level, msg, ref=None: out.append({"module": module, "level": level, "msg": msg, "ref": ref})
    for m in modules:
        for fd in m["fields"]:
            if fd.get("required") and _f(draft, fd["id"]) is None:
                add(m["id"], "missing", f"「{fd['label']}」還沒填")
    line = _f(draft, "line")
    same = [x for x in feats if not line or x["line"] == line]
    age_max = _num(_f(draft, "age_max"))
    if (age_max is not None and age_max >= 65) or _f(draft, "senior") is True:
        add("sales", "warn", "開放 65 歲以上投保：要規劃高齡客戶保護（適合度、關懷提問；2026 年起錄音錄影可申請以其他措施取代）",
            "保險業招攬及核保理賠辦法（第 6、7 條，以現行條文為準）")
    if line in ("變額壽險", "變額萬能壽險"):
        add("benefits", "warn", "變額壽險類：身故給付對保單帳戶價值要符合依年齡訂定的最低比率，投保與每次繳費時都要檢核",
            "投資型人壽保險商品死亡給付對保單帳戶價值之最低比率規範")
        if _f(draft, "death_type") == "返還保單帳戶價值（年金型）":
            add("benefits", "warn", "險種是變額壽險，但身故給付型態選了年金型的「返還保單帳戶價值」")
    if line == "變額年金保險" and _f(draft, "annuity_guarantee") is None:
        add("benefits", "info", "變額年金：年金保證期間還沒定（同險種競品多半提供 10／15／20 年）")
    cur = _f(draft, "currency", [])
    if any(c != "新臺幣" for c in cur):
        add("positioning", "info", "外幣計價：要揭露匯率風險，並確認外幣收付作業",
            "投資型保險商品銷售應注意事項（匯率風險揭露）")
    if _f(draft, "distribution") is True:
        add("investment", "warn", "有資產撥回：撥回可能來自本金，銷售文件與條款要明確揭露撥回來源與可能減損本金",
            "投資型保險投資管理辦法；投資型保險商品銷售應注意事項")
    if _f(draft, "discretionary") is True or "全權委託帳戶" in _f(draft, "asset_types", []):
        add("investment", "info", "全權委託帳戶：要符合委託投資的資格、費用揭露與專設帳簿規定", "投資型保險投資管理辦法")
    ch = _f(draft, "channel", [])
    if "網路投保" in ch:
        add("sales", "info", "網路投保：確認可網路銷售的商品範圍與身分驗證", "保險業辦理電子商務應注意事項")
    if "銀行保險" in ch:
        add("sales", "info", "銀行通路：確認銀行保險業務的招攬、說明與適合度規定",
            "銀行、保險公司、保險代理人或保險經紀人辦理銀行保險業務應注意事項")
    for fid, feat, label in (("premium_charge", "premium_charge_pct", "保費費用率"), ("surrender_max", "surrender_max_pct", "第一年解約費用率")):
        v = _num(_f(draft, fid))
        comp = [x["features"].get(feat) for x in same if x["features"].get(feat) is not None]
        if v is not None and comp:
            if v > max(comp):
                add("fees", "info", f"{label} {v:g}% 高於同險種競品最高 {max(comp):g}%（{len(comp)} 個商品）")
            elif v < min(comp):
                add("fees", "info", f"{label} {v:g}% 低於同險種競品最低 {min(comp):g}%（{len(comp)} 個商品）")
    if _f(draft, "filing_type") in (None, "待確認"):
        add("filing", "info", "送審方式（核准／備查）還沒確定", "保險商品銷售前程序作業準則")
    return out


# ====================================================================== 草案儲存
class DraftStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def conn(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE IF NOT EXISTS drafts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, data TEXT NOT NULL,
                        base TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def list(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT id, name, base, data, created_at, updated_at FROM drafts ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            out.append({"id": r["id"], "name": r["name"], "base": r["base"], "line": (d.get("fields") or {}).get("line"),
                        "created_at": r["created_at"], "updated_at": r["updated_at"]})
        return out

    def get(self, did: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "name": r["name"], "base": r["base"], "created_at": r["created_at"],
                "updated_at": r["updated_at"], **json.loads(r["data"])}

    def create(self, name: str, data: dict[str, Any] | None = None, base: str | None = None) -> dict[str, Any]:
        data = clean_data(data or {})
        now = self._now()
        with self.conn() as c:
            did = c.execute("INSERT INTO drafts (name, data, base, created_at, updated_at) VALUES (?,?,?,?,?)",
                            (name.strip() or "未命名商品", json.dumps(data, ensure_ascii=False), base, now, now)).lastrowid
        return self.get(did)

    def update(self, did: int, name: str | None, data: dict[str, Any]) -> dict[str, Any] | None:
        if self.get(did) is None:
            return None
        with self.conn() as c:
            c.execute("UPDATE drafts SET name=COALESCE(?, name), data=?, updated_at=? WHERE id=?",
                      (name.strip() if name else None, json.dumps(clean_data(data), ensure_ascii=False), self._now(), did))
        return self.get(did)

    def delete(self, did: int) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM drafts WHERE id=?", (did,)).rowcount > 0


def clean_data(data: dict[str, Any]) -> dict[str, Any]:
    """只留 fields（欄位值）、notes（各模組備註）、refs（釘選到規格書的參考）。"""
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
    refs = data.get("refs") if isinstance(data.get("refs"), dict) else {}
    ok = lambda v: isinstance(v, (str, int, float, bool)) or v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v))
    return {"fields": {str(k)[:64]: v for k, v in fields.items() if ok(v)},
            "notes": {str(k)[:64]: str(v)[:5000] for k, v in notes.items()},
            "refs": {str(k)[:64]: [r for r in v if isinstance(r, dict)][:50] for k, v in refs.items() if isinstance(v, list)}}


def draft_from_product(item: dict[str, Any]) -> dict[str, Any]:
    """從競品複製：把擷取到的欄位填進草案，並把來源商品釘選到「商品定位」。"""
    f = item["features"]
    fields = {
        "name": f"（參考 {item['name']}）", "line": f.get("line"), "currency": f.get("currency"),
        "payment": f.get("payment_modes"), "death_type": (f.get("death_type") or [None])[0],
        "min_death": f.get("min_death_guarantee"), "disability": f.get("disability"),
        "maturity": f.get("maturity_benefit"), "annuity_guarantee": f.get("annuity_guarantee_years"),
        "accumulation_min": f.get("accumulation_min_years"), "discretionary": f.get("discretionary"),
        "distribution": f.get("distribution"), "stop_profit": f.get("stop_profit"),
        "premium_charge": f.get("premium_charge_pct"), "surrender_max": f.get("surrender_max_pct"),
        "bonus": f.get("bonus"), "partial_withdrawal": f.get("partial_withdrawal"),
        "basic_amount_change": f.get("basic_amount_change"), "riders": f.get("riders"),
    }
    ref = {"kind": "product", "title": f"{item['company']}｜{item['name']}", "url": item.get("pdf"), "note": "複製來源"}
    return {"fields": {k: v for k, v in fields.items() if v not in (None, [])}, "notes": {}, "refs": {"positioning": [ref]}}


# ====================================================================== 各模組情境
def _match(title: str, kws: list[str]) -> list[str]:
    return [k for k in kws if k and k in (title or "")]


def context(conn, module: dict[str, Any], draft: dict[str, Any] | None, feats: list[dict[str, Any]],
            self_company: str | None, market: Callable[[str], Any], product: str | None = None) -> dict[str, Any]:
    draft = draft or {"fields": {}}
    line = _f(draft, "line")
    same = [x for x in feats if not line or x["line"] == line]
    same_sorted = sorted(same, key=lambda x: (x["first_date"] or ""), reverse=True)
    # 競品：欄位分布
    fsum = {fd["id"]: summarize(same, fd["feature"], self_company) for fd in module["fields"] if fd.get("feature")}
    # 競品：選一個商品看條文（預設最新核准的他家商品）
    sel = None
    if product:
        co, _, nm = product.partition("|")
        sel = next((x for x in feats if x["company"] == co and x["name"] == nm), None)
    if sel is None:   # 預設：這個模組擷取到最多欄位的他家商品中，最新核准的一個
        feats_ids = [fd["feature"] for fd in module["fields"] if fd.get("feature")]
        score = lambda x: sum(x["features"].get(k) not in (None, [], False) for k in feats_ids)
        others = [x for x in same_sorted if x["company"] != self_company] or same_sorted
        sel = max(others, key=lambda x: (score(x), x["first_date"] or "")) if others else None
    articles = []
    if sel:
        kws, akws = module.get("articles") or [], module.get("appendix") or []
        for r in conn.execute("""SELECT seq, kind, article_no, title, text, page_start FROM clause_articles
                                 WHERE raw_doc_id=? ORDER BY seq""", (sel["raw_doc_id"],)):
            if r["kind"] == "article" and _match(r["title"], kws):
                articles.append({"kind": "article", "title": f"第 {r['article_no']} 條 {r['title']}", "text": r["text"][:2000],
                                 "page": r["page_start"]})
            elif r["kind"] == "appendix" and akws:
                for k in akws:
                    i = r["text"].find(k)
                    if i >= 0:
                        a, b = max(0, r["text"].rfind("\n", 0, max(0, i - 200)) + 1), min(len(r["text"]), i + 1400)
                        articles.append({"kind": "appendix", "title": f"附表：{k}", "text": r["text"][a:b], "page": r["page_start"]})
                        break
    compet = {"line": line, "n": len(same), "self_n": sum(x["company"] == self_company for x in same), "fields": fsum,
              "products": [{"company": x["company"], "name": x["name"], "first_date": x["first_date"],
                            "features": {fd["id"]: x["features"].get(fd["feature"]) for fd in module["fields"] if fd.get("feature")},
                            "pdf": x["pdf"]} for x in same_sorted],
              "selected": None if not sel else {"company": sel["company"], "name": sel["name"], "pdf": sel["pdf"],
                                                "first_date": sel["first_date"]},
              "articles": articles}
    # 法規：靜態清單＋法規動態標題比對
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    law, news = [], []
    if "doc_labels" in tables:
        srcs = ["tii_law_rss", "fsc_press"] + (["fsc_penalty"] if module.get("include_penalties") else [])
        q = ",".join("?" * len(srcs))
        for r in conn.execute(f"""SELECT l.raw_doc_id, l.source_id, l.title, l.published_at, l.impact, d.raw_path, d.url
                                  FROM doc_labels l JOIN raw_docs d ON d.id = l.raw_doc_id
                                  WHERE l.source_id IN ({q}) AND l.hidden = 0
                                  ORDER BY l.published_at DESC""", srcs):
            hit = _match(r["title"], module.get("law_keywords") or [])
            if hit and not _match(r["title"], LAW_EXCLUDE):
                law.append({"title": r["title"], "date": (r["published_at"] or "")[:10], "source": r["source_id"],
                            "impact": r["impact"], "url": r["url"], "raw": f"/{r['raw_path']}", "hit": hit})
        for r in conn.execute("""SELECT l.title, l.published_at, l.impact, l.categories, d.url FROM doc_labels l
                                 JOIN raw_docs d ON d.id = l.raw_doc_id
                                 WHERE l.source_id = 'news_rss' AND l.hidden = 0 ORDER BY l.published_at DESC"""):
            hit = _match(r["title"], module.get("news_keywords") or [])
            if hit:
                news.append({"title": r["title"], "date": (r["published_at"] or "")[:10], "impact": r["impact"],
                             "url": r["url"], "hit": hit, "categories": json.loads(r["categories"])})
    views = [v for v in (market_view(k, market, line, self_company) for k in module.get("market") or []) if v]
    return {"module": module["id"], "competitors": compet, "regs": module.get("regs") or [],
            "law": law[:15], "news": news[:15], "market": views}


# ====================================================================== 市場數據小圖
def market_view(key: str, market: Callable[[str], Any], line: str | None, self_co: str | None) -> dict[str, Any] | None:
    if key in ("line_trend", "currency_trend", "new_approvals", "revision_waves"):
        s = market("supply")
        if not s.get("as_of"):
            return None
        if key == "line_trend":
            rows = [{"label": c["company"], "parts": c["new_12m_lines"], "note": f"{c['new_12m']} 個"} for c in s["companies"]]
            tot = sum(c["new_12m"] for c in s["companies"])
            n_line = sum(c["new_12m_lines"].get(line, 0) for c in s["companies"]) if line else None
            return {"key": key, "kind": "stack100", "title": "近 12 個月各家新核准商品的險種組成",
                    "series": ["變額年金保險", "變額萬能壽險", "變額壽險"], "rows": rows, "self": self_co,
                    "note": (f"近 12 個月 {tot} 個新品中，{line} {n_line} 個（{round(n_line * 100 / tot) if tot else 0}%）。" if line else ""),
                    "source": f"各公司法定公開商品清單，截至 {s['as_of']}", "link": "/market.html"}
        if key == "currency_trend":
            items = [{"label": c["company"], "a": round(c["foreign"] * 100 / c["on_sale"]) if c["on_sale"] else 0,
                      "b": round(c["new_12m_foreign"] * 100 / c["new_12m"]) if c["new_12m"] else 0} for c in s["companies"]]
            return {"key": key, "kind": "dumbbell", "title": "外幣商品占比：銷售中 → 近 12 個月新品", "items": items,
                    "self": self_co, "unit": "%", "source": f"各公司法定公開商品清單，截至 {s['as_of']}", "link": "/market.html"}
        if key == "new_approvals":
            rec = [p for p in s["recent"] if not line or p["line"] == line][:12]
            return {"key": key, "kind": "list", "title": f"最近 180 天核准的{line or '投資型'}商品",
                    "rows": [[p["first_date"], p["company"], p["name"], p.get("currency") or ""] for p in rec],
                    "head": ["核准日", "公司", "商品", "幣別"], "source": f"各公司法定公開商品清單，截至 {s['as_of']}",
                    "link": "/market.html"}
        if key == "revision_waves":
            return {"key": key, "kind": "list", "title": "條款修正集中的月份（主管機關要求的全面修正會集中出現）",
                    "rows": [[w["month"], w["count"], "、".join(f"{c} {n}" for c, n in w["companies"].items()),
                              f"{w['top_doc'][0]}（{w['top_doc'][1]} 件）" if w["top_doc"] else "各自修正"]
                             for w in s["revision_waves"]],
                    "head": ["月份", "件數", "公司", "同一天的金管會文號"], "source": "各公司法定公開商品清單", "link": "/market.html"}
    if key == "annuity_demand":
        d = market("demand")
        if not d.get("as_of"):
            return None
        rows = [r for r in d["ytd"] if r["line"] != "合計"]
        return {"key": key, "kind": "hbars", "title": f"各險種保費收入年增率（{d['ytd_label']}，全市場）",
                "items": [{"label": r["line"], "value": r["growth"], "strong": r["line"] == "個人年金"} for r in rows],
                "unit": "%", "signed": True, "note": (d["conclusions"][1] if len(d["conclusions"]) > 1 else ""),
                "source": "保險事業發展中心 人身保險業保費收入", "link": "/market.html"}
    if key in ("expense_ratio", "persistency"):
        c = market("companies")
        if not c.get("period"):
            return None
        names = ["新契約費用率"] if key == "expense_ratio" else ["繼續率(十三個月)", "繼續率(二十五個月)"]
        out = []
        for ind in c["indicators"]:
            if ind["name"] in names:
                out.append({"name": ind["name"], "median": ind["median"], "n": ind["n"],
                            "items": [{"label": co, "value": v["value"], "rank": v["rank"], "strong": co == self_co}
                                      for co, v in sorted(ind["values"].items(), key=lambda kv: kv[1]["rank"])]})
        if not out:
            return None
        return {"key": key, "kind": "indicators", "title": f"各公司{'、'.join(names)}（{c['period']}）", "indicators": out,
                "source": "保險局 壽險財務業務指標（政府資料開放平臺 7191）", "link": "/market.html"}
    return None


# ====================================================================== 規格書
def spec(draft: dict[str, Any], modules: list[dict[str, Any]], feats: list[dict[str, Any]],
         self_company: str | None) -> dict[str, Any]:
    line = _f(draft, "line")
    same = [x for x in feats if not line or x["line"] == line]
    mods = []
    for m in modules:
        rows = []
        for fd in m["fields"]:
            rows.append({"id": fd["id"], "label": fd["label"], "value": (draft.get("fields") or {}).get(fd["id"]),
                         "competitors": summarize(same, fd["feature"], self_company) if fd.get("feature") else None})
        mods.append({"id": m["id"], "name": m["name"], "fields": rows, "regs": m.get("regs") or [],
                     "note": (draft.get("notes") or {}).get(m["id"], ""), "refs": (draft.get("refs") or {}).get(m["id"], [])})
    return {"draft": {k: draft[k] for k in ("id", "name", "base", "created_at", "updated_at") if k in draft},
            "line": line, "n_competitors": len(same), "modules": mods, "checks": checks(draft, modules, feats)}
