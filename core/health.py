"""健康檢查（Handbook「可靠性與監控」）：網站改版時要大聲失敗，不能默默失效。

每輪抓取結束後檢查：
- zero_listed：連續 N 輪（預設 3）items_listed = 0。本來就常為 0 的來源
  （例如以關鍵字過濾的中央社）在 sources.yaml 設 allow_empty_runs: true 略過此項。
- missing_fields：本輪新寫入的文件中，解析不到必要欄位（meta.parse_warnings）的比例 > 30%。
- run_errors：本輪狀態為 error（列表頁失敗）或 partial（部分項目／feed 失敗）。

警示寫入 alerts 表並以 WARNING 記錄；狀況恢復時自動解除。推送 Email／Teams 留待後續里程碑。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from core.storage import Database, iso, utcnow

log = logging.getLogger(__name__)

ZERO_LISTED = "zero_listed"
MISSING_FIELDS = "missing_fields"
RUN_ERRORS = "run_errors"


@dataclass
class HealthResult:
    raised: list[tuple[str, str]]    # (kind, message)：本輪觸發中的警示
    resolved: list[str]              # 本輪解除的 kind


def _raise(db: Database, source_id: str, kind: str, message: str) -> bool:
    """回傳 True 表示這是新警示（之前沒有未解除的同類警示）。"""
    now = iso(utcnow())
    row = db.conn.execute(
        "SELECT id FROM alerts WHERE source_id=? AND kind=? AND resolved_at IS NULL",
        (source_id, kind)).fetchone()
    if row:
        db.conn.execute(
            "UPDATE alerts SET last_seen_at=?, occurrences=occurrences+1, message=? WHERE id=?",
            (now, message, row["id"]))
        return False
    db.conn.execute(
        "INSERT INTO alerts (source_id, kind, message, first_seen_at, last_seen_at) VALUES (?,?,?,?,?)",
        (source_id, kind, message, now, now))
    return True


def _resolve(db: Database, source_id: str, kind: str) -> bool:
    cur = db.conn.execute(
        "UPDATE alerts SET resolved_at=? WHERE source_id=? AND kind=? AND resolved_at IS NULL",
        (iso(utcnow()), source_id, kind))
    return cur.rowcount > 0


def check_after_run(db: Database, source_id: str, config: dict[str, Any]) -> HealthResult:
    zero_runs = int(config.get("health_zero_runs", 3))
    missing_ratio = float(config.get("health_missing_ratio", 0.3))
    allow_empty = bool(config.get("allow_empty_runs", False))

    runs = db.conn.execute(
        "SELECT * FROM crawl_runs WHERE source_id=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (source_id, max(zero_runs, 1))).fetchall()
    raised: list[tuple[str, str]] = []
    resolved: list[str] = []
    if not runs:
        return HealthResult(raised, resolved)
    last = runs[0]

    def handle(kind: str, problem: str | None) -> None:
        if problem:
            raised.append((kind, problem))
            if _raise(db, source_id, kind, problem):
                log.warning("ALERT [%s] %s: %s", source_id, kind, problem)
        elif _resolve(db, source_id, kind):
            resolved.append(kind)
            log.info("resolved [%s] %s", source_id, kind)

    # 1. 連續 N 輪 0 筆
    zero_problem = None
    if not allow_empty and len(runs) >= zero_runs and all(r["items_listed"] == 0 for r in runs):
        zero_problem = f"連續 {zero_runs} 輪列表為 0 筆，可能網站改版或被擋"
    handle(ZERO_LISTED, zero_problem)

    # 2. 必要欄位空值比例
    docs = db.conn.execute(
        "SELECT meta FROM raw_docs WHERE source_id=? AND fetched_at >= ?",
        (source_id, last["started_at"])).fetchall()
    missing_problem = None
    if docs:
        bad = sum(1 for d in docs if json.loads(d["meta"]).get("parse_warnings"))
        if bad / len(docs) > missing_ratio:
            missing_problem = f"本輪 {len(docs)} 筆中有 {bad} 筆解析不到必要欄位（>{missing_ratio:.0%}），可能網站改版"
    handle(MISSING_FIELDS, missing_problem)

    # 3. 本輪錯誤
    err_problem = None
    if last["status"] in ("error", "partial"):
        first_line = (last["error_detail"] or "").splitlines()[0][:300] if last["error_detail"] else ""
        err_problem = f"本輪狀態 {last['status']}（{last['errors']} 個錯誤）：{first_line}"
    handle(RUN_ERRORS, err_problem)

    db.conn.commit()
    return HealthResult(raised, resolved)


def summary(db: Database, source_ids: list[str]) -> list[dict[str, Any]]:
    """各來源最後執行、最後成功時間與未解除警示（Handbook：簡單頁面列出各來源狀態）。"""
    out = []
    for sid in source_ids:
        last = db.conn.execute(
            "SELECT * FROM crawl_runs WHERE source_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        ok = db.conn.execute(
            "SELECT finished_at FROM crawl_runs WHERE source_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
        alerts = db.conn.execute(
            "SELECT kind, message, occurrences, first_seen_at FROM alerts "
            "WHERE source_id=? AND resolved_at IS NULL ORDER BY id", (sid,)).fetchall()
        out.append({
            "source_id": sid,
            "last_run": last["started_at"] if last else None,
            "last_status": last["status"] if last else "never",
            "last_listed": last["items_listed"] if last else None,
            "last_new": last["items_new"] if last else None,
            "last_success": ok["finished_at"] if ok else None,
            "open_alerts": [dict(a) for a in alerts],
        })
    return out
