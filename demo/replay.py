"""重播事件（M4，D24）：從快照依真實日期重建「過去 N 天」的情報流，逐筆送進解析層。

Handbook「Demo 策略」：現場不即時爬網站，用快照＋重播展示完整流程（抓取 → 事件 → 解析 → 分類）。

    python -m demo.replay demo-1004                   # 過去 90 天，一次跑完
    python -m demo.replay demo-1004 --days 180 --delay 0.8   # 每筆間隔 0.8 秒，適合現場
    python -m demo.replay demo-1004 --step            # 每筆按 Enter 才繼續
    python -m demo.replay demo-1004 --only-high       # 畫面只顯示高影響（其餘照常處理）

時間線怎麼來（D24：依真實日期重建，畫面與 payload 都標明）：
- 文字來源（保發中心、金管會、裁罰、新聞）：依 published_at 排序。
- 商品（M3）：第一次執行只建基準，所以快照裡沒有真的上架／改版事件。重播時依清單 PDF 的
  「首次核准／備查日」與「最近一次修正日」重建：
    首次日期落在區間內 → product_launched（reconstructed_from=first_date）＋條款解析
    最近修正日落在區間內 → doc_revised（reconstructed_from=latest_date），沒有前一版 PDF，所以不做逐條比對，
                            改列條款前言的文號
  快照裡若有真的 doc_revised／product_launched／product_discontinued，照原樣重播（reconstructed=false）。
- 區間開始前的項目先「建立基準」（照樣解析、不逐筆顯示）。

重播在 data/replay/<快照>/ 的工作副本上進行，快照本身不動；raw 直接讀快照裡的檔案。
建快照前先跑一次 `python -m scheduler.run --parse`，條款解析結果會帶進快照，重播時不用重新解析 PDF
（否則 250 份條款約需 4 分鐘）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core import events
from core.config import load_config, resolve
from core.storage import Database, RawStore
from demo.snapshot import resolve_snapshot

TPE = ZoneInfo("Asia/Taipei")
# 重播時重新產生的處理層；clause_docs／clause_articles 保留（可重建、但重算很慢）
RESET_TABLES = ["event_processing", "doc_labels", "product_terms", "clause_diffs"]


@dataclass
class Step:
    at: str                      # ISO 日期（Asia/Taipei 當天）
    order: int                   # 同一天內的穩定順序
    type: str
    raw_doc_id: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    baseline: bool = False


def _day(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(TPE).date().isoformat() if "T" in s else s[:10]
    except ValueError:
        return s[:10]


def build_timeline(conn: sqlite3.Connection, start: str, end: str) -> list[Step]:
    """原始事件 → 依真實日期排序的重播步驟。start／end 為 YYYY-MM-DD（含）。"""
    conn.row_factory = sqlite3.Row
    steps: list[Step] = []
    rows = conn.execute("""SELECT e.*, r.meta AS meta FROM events e LEFT JOIN raw_docs r ON r.id = e.raw_doc_id
                           ORDER BY e.id""").fetchall()
    for i, e in enumerate(rows):
        pl = json.loads(e["payload"])
        meta = json.loads(e["meta"] or "{}")
        src = pl.get("source_id", "")
        base = {**pl, "replay": True, "reconstructed": False}
        if e["type"] == events.NEW_ITEM and src.startswith("company_"):
            first, latest = meta.get("first_date"), meta.get("latest_date")
            if first and first >= start:
                steps.append(Step(first, i * 3, events.PRODUCT_LAUNCHED, None, {
                    "source_id": src, "item_key": pl.get("item_key"), "title": pl.get("title"),
                    "company": meta.get("company"), "line": meta.get("line"), "currency": meta.get("currency"),
                    "first_date": first, "latest_date": latest, "replay": True, "reconstructed": True,
                    "reconstructed_from": "first_date"}))
                steps.append(Step(first, i * 3 + 1, events.NEW_ITEM, e["raw_doc_id"], base))
            elif latest and latest >= start and latest != first:
                steps.append(Step(latest, i * 3, events.NEW_ITEM, e["raw_doc_id"], base, baseline=True))
                steps.append(Step(latest, i * 3 + 1, events.DOC_REVISED, e["raw_doc_id"], {
                    **base, "reconstructed": True, "reconstructed_from": "latest_date",
                    "previous_raw_doc_id": None, "revision_date": latest}))
            else:
                steps.append(Step(first or latest or start, i * 3, events.NEW_ITEM, e["raw_doc_id"], base,
                                  baseline=True))
            continue
        if e["type"] == events.NEW_ITEM:
            at = _day(pl.get("published_at")) or _day(e["created_at"])
        else:                                               # 真的改版／上架／停售：用發生時間
            at = _day(e["created_at"])
        steps.append(Step(at, i * 3, e["type"], e["raw_doc_id"], base, baseline=at < start))
    steps = [s for s in steps if s.at <= end]
    steps.sort(key=lambda s: (not s.baseline, s.at, s.order))   # 基準先、再依日期
    for s in steps:
        if s.at < start:
            s.baseline = True
    return steps


def prepare_workdir(snap: Path, cfg: dict) -> Path:
    work = resolve(cfg["defaults"]["db_path"]).parent / "replay" / snap.name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy2(snap / "intel.db", work / "intel.db")
    db = Database(work / "intel.db")
    db.init_schema()                                   # 舊快照沒有 M4 的表也能補上
    for t in RESET_TABLES:
        db.conn.execute(f"DELETE FROM {t}")
    db.conn.execute("DELETE FROM events")
    db.conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
    db.conn.commit()
    db.close()
    return work


def _line(step: Step, status: str, summary: dict[str, Any]) -> str:
    pl = step.payload
    title = pl.get("title") or pl.get("item_key")
    rec = "（依核准日重建）" if pl.get("reconstructed_from") == "first_date" else \
          "（依修正日重建）" if pl.get("reconstructed_from") == "latest_date" else ""
    if status != "ok":
        return f"{step.at}  ⚠ {step.type} {status} {title} {summary.get('error', '')}"
    if step.type == events.PRODUCT_LAUNCHED:
        return f"{step.at}  ＋上架  {pl.get('company')}｜{title}{rec}"
    if step.type == events.PRODUCT_DISCONTINUED:
        return f"{step.at}  －停售  {pl.get('company')}｜{title}"
    if "impact" in summary:
        mark = {"high": "★高", "medium": "・中", "low": "　低"}[summary["impact"]]
        cats = "/".join(summary.get("categories") or []) or "未分類"
        return f"{step.at}  {mark} [{cats}] {title}"
    if "articles" in summary:
        if summary.get("not_main_clause"):
            return f"{step.at}  ⚠ 條款對應疑似錯誤（批註條款或其他商品的條款）｜{title}"
        miss = len(summary.get("missing", []))
        head = ("條款來源更正" if summary.get("source_corrected") else
                "條款改版" if step.type == events.DOC_REVISED else "條款解析")
        extra = ""
        if summary.get("diff"):
            d = summary["diff"]
            extra = f"；改 {len(d['changed'])} 條、增 {len(d['added'])}、刪 {len(d['removed'])}"
        elif step.type == events.DOC_REVISED and pl.get("revision_date"):
            docs = "、".join(f"{f.get('kind') or ''}{f['doc_no']}" for f in summary.get("filings") or [])
            extra = f"；修正日 {pl.get('revision_date')}{rec}" + (f"，{docs}" if docs else "")
        return f"{step.at}  　{head} {summary['articles']} 條，欄位 {summary.get('fields', 0)}，待補 {miss}{extra}｜{title}"
    return f"{step.at}  　{step.type} {title}"


def replay(snapshot_name: str, cfg: dict | None = None, *, days: int = 90, as_of: str | None = None,
           delay: float = 0.0, step_mode: bool = False, only_high: bool = False,
           out: Callable[[str], None] = print) -> dict[str, Any]:
    from parsers.base import ParseContext, load_enricher
    from parsers.pipeline import default_handlers, process_pending

    cfg = cfg or load_config()
    snap = resolve_snapshot(snapshot_name, cfg)
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    end = as_of or datetime.fromisoformat(manifest["created_at"]).astimezone(TPE).date().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    work = prepare_workdir(snap, cfg)

    src_conn = sqlite3.connect(f"file:{snap / 'intel.db'}?mode=ro", uri=True)
    steps = build_timeline(src_conn, start, end)
    src_conn.close()
    pre_parsed = manifest["counts"].get("clause_docs", 0)

    db = Database(work / "intel.db")
    pcfg = cfg.get("parsing", {}) or {}
    ctx = ParseContext(db=db, raw=RawStore(snap / "raw"), config=pcfg, enricher=load_enricher(pcfg.get("llm")))
    handlers = default_handlers(ctx)
    log = open(work / "replay_log.jsonl", "w", encoding="utf-8")
    stats: dict[str, Any] = {"baseline": 0, "replayed": 0, "by_type": {}, "high": [], "errors": 0}

    def run(step: Step) -> tuple[str, dict[str, Any]]:
        step.payload["replay_at"] = step.at
        events.emit(db, step.type, step.raw_doc_id, step.payload)
        db.conn.commit()
        got: list[tuple[str, dict]] = []
        process_pending(ctx, handlers, limit=1, on_event=lambda e, s, sm: got.append((s, sm)))
        status, summary = got[0] if got else ("skipped", {})
        log.write(json.dumps({"at": step.at, "type": step.type, "baseline": step.baseline, "status": status,
                              "title": step.payload.get("title"), "summary": summary}, ensure_ascii=False,
                             default=str) + "\n")
        return status, summary

    baseline = [s for s in steps if s.baseline]
    live = [s for s in steps if not s.baseline]
    out(f"快照 {manifest['name']}（{manifest['created_at'][:10]}）｜重播區間 {start} ～ {end}（{days} 天）")
    if not pre_parsed:
        out("⚠ 快照沒有預先解析的條款，建立基準時要逐份解析 PDF，會比較久"
            "（建快照前先跑 python -m scheduler.run --parse）")
    out(f"建立基準：{len(baseline)} 筆（區間開始前的公告與現有商品）…")
    t0 = time.time()
    for s in baseline:
        status, _ = run(s)
        stats["baseline"] += 1
        stats["errors"] += status in ("error", "failed")
    out(f"基準完成（{time.time() - t0:.1f} 秒）。開始重播 {len(live)} 筆事件：\n")

    for s in live:
        status, summary = run(s)
        stats["replayed"] += 1
        stats["by_type"][s.type] = stats["by_type"].get(s.type, 0) + 1
        stats["errors"] += status in ("error", "failed")
        if summary.get("impact") == "high":
            stats["high"].append(f"{s.at} {s.payload.get('title')}")
        hidden = summary.get("hidden") or (only_high and summary.get("impact") != "high"
                                           and s.type not in (events.PRODUCT_LAUNCHED, events.PRODUCT_DISCONTINUED))
        if not hidden:
            out(_line(s, status, summary))
            if step_mode:
                input()
            elif delay:
                time.sleep(delay)
    log.close()

    terms = db.conn.execute("SELECT COUNT(*), SUM(clause_raw_doc_id IS NOT NULL) FROM product_terms").fetchone()
    stats["product_terms"] = {"products": terms[0], "with_clause": terms[1] or 0}
    stats["labels"] = dict(db.conn.execute("SELECT impact, COUNT(*) FROM doc_labels GROUP BY impact").fetchall())
    stats["workdir"] = str(work)
    db.close()
    out(f"\n重播完成：基準 {stats['baseline']} 筆、重播 {stats['replayed']} 筆 {stats['by_type']}，"
        f"錯誤 {stats['errors']}")
    out(f"分類：{stats['labels']}；統一商品 schema {stats['product_terms']['products']} 個商品"
        f"（{stats['product_terms']['with_clause']} 個有條款）")
    out(f"工作資料庫：{work / 'intel.db'}")
    out(f"  查詢：python -m scheduler.run --db \"{work / 'intel.db'}\" --labels（或 --terms、--products）")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="從快照重播事件（demo 用）")
    ap.add_argument("snapshot")
    ap.add_argument("--days", type=int, default=90, help="重播最近幾天（預設 90）")
    ap.add_argument("--as-of", help="區間結束日 YYYY-MM-DD（預設快照建立日）")
    ap.add_argument("--delay", type=float, default=0.0, help="每筆顯示後暫停秒數")
    ap.add_argument("--step", action="store_true", help="每筆按 Enter 才繼續")
    ap.add_argument("--only-high", action="store_true", help="畫面只顯示高影響與上架／停售")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    s = replay(a.snapshot, load_config(a.config), days=a.days, as_of=a.as_of, delay=a.delay,
               step_mode=a.step, only_high=a.only_high)
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
