"""把網站匯出成靜態快照（發佈成 claude.ai 頁面用，D31）。

    python scripts/export_site.py                 # 讀 data/intel.db，輸出到 data/site/
    python scripts/export_site.py --out 路徑 --db 路徑

做法：把前端會呼叫的 API 結果先算好存成 JSON（data/*.json），頁面改由 staticapi.js 讀這些檔案；
需要依條件篩選的（情報牆、商品清單）在瀏覽器端篩。商品工作台的草案改存在 claude.ai 頁面的共用資料庫。
原始檔（/raw/…）不隨快照發佈：連結一律換成原始來源網址（條款 PDF 換成保險公司官網的檔案網址）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config, resolve          # noqa: E402
from web.server import STATIC, build_store              # noqa: E402

PAGES = ["lab.html", "wall.html", "compare.html", "market.html", "spec.html"]
ASSETS = ["style.css", "common.js", "charts.js", "staticapi.js"]
LINES = ["", "變額年金保險", "變額壽險", "變額萬能壽險"]


def raw_url_map(db: Path) -> dict[str, str]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for raw_path, url, meta in c.execute("SELECT raw_path, url, meta FROM raw_docs"):
        m = json.loads(meta or "{}")
        target = m.get("final_url") or url
        if raw_path and target:
            out["/" + raw_path] = target
    c.close()
    return out


def relink(obj, m: dict[str, str]):
    """把 /raw/... 連結換成原始來源網址（保留 #page=N）；找不到對應就拿掉連結。"""
    if isinstance(obj, dict):
        return {k: relink(v, m) for k, v in obj.items()}
    if isinstance(obj, list):
        return [relink(v, m) for v in obj]
    if isinstance(obj, str) and obj.startswith("/raw/"):
        base, _, frag = obj.partition("#")
        url = m.get(base)
        return (url + ("#" + frag if frag else "")) if url else None
    return obj


def page_html(name: str, main: bool) -> str:
    s = (STATIC / name).read_text(encoding="utf-8")
    # 絕對路徑 → 相對路徑（頁面與資料檔放在同一層）
    s = s.replace('href="/style.css"', 'href="style.css"')
    s = s.replace('src="/common.js"></script>', 'src="common.js"></script>\n<script src="staticapi.js"></script>')
    s = s.replace('src="/charts.js"', 'src="charts.js"')
    s = re.sub(r'(["\'`])/(wall|compare|market|spec)\.html', r'\1\2.html', s)
    s = re.sub(r'(["\'`])/#', r'\1index.html#', s)
    s = s.replace('<a id="lnkSpec" class="btn primary" target="_blank">', '<a id="lnkSpec" class="btn primary">')
    s = s.replace('\'" target="_blank">看規格書 →</a>\'', '\'">看規格書 →</a>\'')
    if main:   # 發佈後的頁面名稱用平台名稱
        s = s.replace("<title>商品工作台</title>", "<title>商品與市場情報平台</title>", 1)
    if main:   # 資料來源頁在主頁的框架裡開（共用資料庫只有主頁拿得到）
        s = s.replace('<nav id="nav"></nav>', '<nav id="nav"></nav>\n<iframe id="srcFrame" class="src-frame" title="資料來源" hidden></iframe>', 1)
    if main:   # 主頁由 Artifact 加上外框：拿掉自己的 doctype/html/head/body
        s = re.sub(r"<!doctype html>\s*", "", s, flags=re.I)
        s = re.sub(r"</?(html|head|body)(\s[^>]*)?>\s*", "", s, flags=re.I)
        s = re.sub(r'<meta charset="utf-8">\s*|<meta name="viewport"[^>]*>\s*', "", s)
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/site")
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    cfg = load_config()
    store = build_store(cfg, a.db)
    out = resolve(a.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    links = raw_url_map(store.db_path)

    def dump(name, obj):
        (out / "data" / name).write_text(json.dumps(relink(obj, links), ensure_ascii=False, separators=(",", ":")),
                                         encoding="utf-8")

    from web.product_lab import context
    meta = store.meta()
    meta["db"] = "快照"
    meta["snapshot_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    dump("meta.json", meta)
    for d in (7, 14, 30):
        dump(f"week_{d}.json", store.week(d))
    labels = store.labels({"hidden": "all", "limit": "2000"})["items"]
    dump("labels.json", labels)
    dump("impact.json", {str(i["raw_doc_id"]): store.impact_detail(i["raw_doc_id"]) for i in labels})
    prods = store.products({})["items"]
    dump("products.json", prods)
    detail = {f"{p['company']}|{p['name']}": store.product(p["company"], p["name"]) for p in prods}
    # 條文依公司分檔（全部放一個檔太大），products_detail 記哪個商品在哪個檔
    companies = sorted({p["company"] for p in prods})
    art_file = {}
    with store.conn() as c:
        for i, co in enumerate(companies):
            ids = [d["clause"]["raw_doc_id"] for d in detail.values() if d and d["company"] == co and d.get("clause")]
            ids += [v["raw_doc_id"] for d in detail.values() if d and d["company"] == co for v in d["versions"]]
            ids = sorted(set(ids))
            arts = {}
            for rid in ids:
                rows = [dict(r) for r in c.execute("""SELECT seq, kind, article_no, title, text, page_start, page_end
                                                      FROM clause_articles WHERE raw_doc_id=? ORDER BY seq""", (rid,))]
                if rows:
                    arts[str(rid)] = rows
                    art_file[str(rid)] = f"articles_{i}.json"
            dump(f"articles_{i}.json", arts)
    dump("products_detail.json", detail)
    dump("articles_index.json", art_file)
    dump("market_supply.json", store.market_supply())
    dump("market_demand.json", store.market_demand())
    dump("market_companies.json", store.market_companies())
    modules = store.lab_modules()
    dump("lab_modules.json", modules)
    with store.conn() as c:
        feats = store.lab_features(c)
        dump("lab_features.json", feats)
        mk = store._market_fn()
        for m in modules:
            for ln in LINES:
                ctx = context(c, m, {"fields": {"line": ln or None}}, feats, store.self_company, mk)
                ctx["competitors"].pop("articles", None)
                dump(f"ctx_{m['id']}_{LINES.index(ln)}.json", ctx)
    for name in PAGES:
        target = "index.html" if name == "lab.html" else name
        (out / target).write_text(page_html(name, name == "lab.html"), encoding="utf-8")
    for name in ASSETS:
        text = (STATIC / name).read_text(encoding="utf-8")
        if name == "common.js":   # 導覽列連結改成相對路徑
            text = text.replace('["lab", "/", "商品工作台"]', '["lab", "index.html", "商品工作台"]')
            text = text.replace('<a class="brand" href="/">', '<a class="brand" href="index.html">')
            text = re.sub(r'"/(wall|compare|market)\.html"', r'"\1.html"', text)
        (out / name).write_text(text, encoding="utf-8")
    n = sum(1 for _ in out.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"匯出完成：{out}（{n} 個檔案，{size / 1e6:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
