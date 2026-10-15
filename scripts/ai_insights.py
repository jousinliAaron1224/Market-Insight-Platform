"""AI 判讀情報對法巴的影響（Ollama Cloud）。爬完、解析完之後跑，結果存在 ai_insights 表，匯出時取代規則判讀。

    python scripts/ai_insights.py              # 判讀還沒判讀過的情報與法規（每次最多 radar.ai_limit 則）
    python scripts/ai_insights.py --redo 5     # 重新判讀最新 5 則（改了 prompt 時用）

- 每則只判讀一次（以 raw_doc_id＋PROMPT_VERSION 為鍵）；改了 prompt 就把 PROMPT_VERSION 加一，舊結果自動不再使用。
- 金鑰：環境變數 OLLAMA_API_KEY，或專案根目錄的 .env（已列入 .gitignore）。沒有金鑰就跳過，網站照舊用規則判讀。
- 模型輸出一定要是合法 JSON 且欄位都在允許的值裡，不合格就丟掉（不寫入），下次再試。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config                    # noqa: E402
from web.server import build_store                     # noqa: E402
from scripts.export_radar import INSURANCE_TITLE, source_text   # noqa: E402
from parsers.gist import clean                         # noqa: E402

PROMPT_VERSION = "v1"
URL = "https://ollama.com/api/chat"
LINES = {"inv": "投資型", "par": "分紅", "mort": "房貸壽險", "usd": "美元利變・年金"}
OWNERS = ["商品開發", "精算", "法遵", "通路", "行銷", "投資管理", "主管"]
DIRS = {"neg": "對法巴不利", "pos": "對法巴有利", "neu": "中性"}
SOURCES = ("fsc_press", "fsc_penalty", "fsc_draft", "news_rss", "tii_law_rss")

SCHEMA = """{"impact_dir":"neg|pos|neu","priority":1到5（5＝法巴這週就要處理）,"lines":["inv|par|mort|usd，可空"],
"why":"為什麼法巴要在意（40 字內）","impact_text":"對法巴的具體影響（90 字內，具體到商品線、通路或作業）",
"owner":"商品開發|精算|法遵|通路|行銷|投資管理|主管 擇一","actions":["建議行動，最多 2 點，每點 30 字內"]}"""


def api_key() -> str | None:
    if os.environ.get("OLLAMA_API_KEY"):
        return os.environ["OLLAMA_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("OLLAMA_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    return None


def ensure_table(db_path: str) -> None:
    c = sqlite3.connect(db_path)
    c.execute("""CREATE TABLE IF NOT EXISTS ai_insights (raw_doc_id INTEGER NOT NULL, prompt_version TEXT NOT NULL,
                 model TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (raw_doc_id, prompt_version))""")
    c.commit()
    c.close()


def load(db_path: str) -> dict[int, dict]:
    """匯出用：raw_doc_id → 判讀結果（只取目前 PROMPT_VERSION）。"""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT raw_doc_id, model, created_at, data FROM ai_insights WHERE prompt_version=?", (PROMPT_VERSION,)).fetchall()
    except sqlite3.OperationalError:   # 還沒跑過，表不存在
        rows = []
    finally:
        c.close()
    return {rid: dict(json.loads(d), model=m, at=at) for rid, m, at, d in rows}


def validate(d: dict) -> dict | None:
    try:
        out = {
            "impact_dir": d["impact_dir"] if d.get("impact_dir") in DIRS else None,
            "priority": max(1, min(5, int(d.get("priority")))),
            "lines": [x for x in (d.get("lines") or []) if x in LINES],
            "why": str(d.get("why") or "").strip()[:80],
            "impact_text": str(d.get("impact_text") or "").strip()[:180],
            "owner": d.get("owner") if d.get("owner") in OWNERS else None,
            "actions": [str(a).strip()[:60] for a in (d.get("actions") or []) if str(a).strip()][:2],
        }
    except (TypeError, ValueError, KeyError):
        return None
    return out if out["impact_dir"] and out["why"] and out["impact_text"] and out["owner"] else None


def ask(key: str, model: str, prompt: str, timeout: int = 120) -> dict | None:
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0.2},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = json.load(r)["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            return validate(json.loads(m.group(0))) if m else None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            if attempt:
                return None
            time.sleep(5)
    return None


def context(store) -> str:
    c = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        n = dict(c.execute("SELECT company, COUNT(*) FROM product_terms WHERE status='on_sale' GROUP BY company").fetchall())
    finally:
        c.close()
    return (f"法國巴黎人壽（法巴）台灣分公司：主力商品是投資型（銷售中 {n.get('法商法國巴黎人壽', 0)} 張）、分紅、房貸壽險，另有美元利變年金；"
            "主要通路是銀行（兆豐、華南、永豐等）；不追求高佣金。主要競爭對手：國泰、富邦、凱基、台灣人壽、南山、安聯。")


def items(store, cfg) -> list[dict]:
    """網站上會出現的情報與法規（同一則公告改版只取最新版），新的在前。"""
    labels = [x for x in store.labels({"hidden": "all", "limit": "2000"})["items"] if not x.get("hidden") and x.get("source_id") in SOURCES]
    latest = {}
    for x in labels:
        k = (x["source_id"], x.get("url") or x["raw_doc_id"])
        if k not in latest or x["raw_doc_id"] > latest[k]["raw_doc_id"]:
            latest[k] = x
    out = [x for x in latest.values() if x["source_id"] != "news_rss" or INSURANCE_TITLE.search(x["title"] or "")]
    return sorted(out, key=lambda x: (x.get("date") or "", x["raw_doc_id"]), reverse=True)


def run(cfg=None, limit: int | None = None, redo: int = 0, log=print) -> dict:
    cfg = cfg or load_config()
    key = api_key()
    if not key:
        return {"skipped": "沒有 OLLAMA_API_KEY"}
    rcfg = cfg.get("radar") or {}
    model = rcfg.get("ai_model", "deepseek-v4-pro:0813")
    limit = limit or int(rcfg.get("ai_limit", 60))
    store = build_store(cfg, None)
    ensure_table(store.db_path)
    done = load(store.db_path)
    todo = items(store, cfg)
    redo_ids = {x["raw_doc_id"] for x in todo[:redo]}
    todo = [x for x in todo if x["raw_doc_id"] not in done or x["raw_doc_id"] in redo_ids][:limit]
    ctx, ok, bad = context(store), 0, 0
    c = sqlite3.connect(store.db_path)
    for x in todo:
        body = clean(source_text(store, x["raw_doc_id"], x["source_id"]))[:3500]
        prompt = (f"你是法巴人壽商品開發部的市場情報分析師。依下列情報判讀對法巴的影響，只回覆一個 JSON 物件，格式：\n{SCHEMA}\n"
                  f"判讀原則：只根據情報內容，不要編造數字；跟法巴無關（例如只影響產險）就 priority 1～2、impact_dir neu。\n\n"
                  f"法巴背景：{ctx}\n\n情報來源：{x.get('source')}　日期：{x.get('date')}\n標題：{x['title']}\n內文：{body or '（只有標題）'}")
        r = ask(key, model, prompt)
        if r:
            c.execute("INSERT OR REPLACE INTO ai_insights VALUES (?,?,?,?,?)", (x["raw_doc_id"], PROMPT_VERSION, model,
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(r, ensure_ascii=False)))
            c.commit()
            ok += 1
        else:
            bad += 1
        log(f"AI 判讀 {'✓' if r else '✗'} {x['title'][:40]}")
    c.close()
    return {"model": model, "analyzed": ok, "failed": bad, "already": len(done)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", type=int, default=0, help="重新判讀最新 N 則")
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, redo=a.redo), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
