"""執行入口：手動跑單一來源、全部跑一輪，或常駐依 sources.yaml 的 cron 排程（APScheduler）。

用法（在專案根目錄）：
    python -m scheduler.run tii_law_rss            # 跑一個來源一輪
    python -m scheduler.run tii_law_rss --refetch  # 已看過的也重新走第 2、3 層
    python -m scheduler.run --all                  # 所有已實作來源依序各跑一輪
    python -m scheduler.run --serve                # 常駐排程（Ctrl+C 結束）
    python -m scheduler.run --health               # 各來源最後成功時間、狀態與未解除警示
    python -m scheduler.run --events               # 列出未處理事件
    python -m scheduler.run --runs                 # 列出最近的 crawl_runs
    python -m scheduler.run --products             # 各公司投資型商品數與最近上架／停售
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from core import events, health
from core.change_detect import run_source, validator_lookup
from core.config import load_adapter_class, load_config, resolve, source_config
from core.context import AdapterContext
from core.http import PoliteClient, RetryPolicy
from core.ratelimit import DomainRateLimiter
from core.storage import Database, RawStore

log = logging.getLogger("scheduler")


def open_db(cfg) -> Database:
    db = Database(resolve(cfg["defaults"]["db_path"]))
    db.init_schema()
    return db


def shared_limiter(cfg) -> DomainRateLimiter:
    """所有來源共用：同網域的並發上限與請求間隔跨來源生效。"""
    d = cfg.get("defaults", {})
    return DomainRateLimiter(d.get("min_interval_sec", 3), d.get("per_domain_concurrency", 1))


def implemented_sources(cfg) -> list[str]:
    return [s["id"] for s in cfg.get("sources", []) if s.get("adapter") and s.get("enabled", True)]


def run_once(source_id: str, refetch: bool = False, config_path: str | None = None,
             limiter: DomainRateLimiter | None = None, cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or load_config(config_path)
    scfg = source_config(cfg, source_id)
    if "adapter" not in scfg:
        raise SystemExit(f"{source_id} 尚未實作 adapter")
    db = open_db(cfg)
    http = PoliteClient(
        scfg["user_agent"],
        limiter or shared_limiter(cfg),
        extra_ca_files=[resolve(f) for f in scfg.get("extra_ca_files", [])],
        x509_strict=bool(scfg.get("x509_strict", True)),
        retry=RetryPolicy.from_config(scfg.get("retry")),
    )
    try:
        ctx = AdapterContext(http=http, raw=RawStore(resolve(scfg["raw_root"])),
                             validators=validator_lookup(db))
        adapter = load_adapter_class(scfg["adapter"])(ctx, scfg)
        stats = run_source(adapter, db, refetch=refetch).as_dict()
        stats["retries"] = http.retries_done
        h = health.check_after_run(db, source_id, scfg)
        stats["alerts"] = [f"{k}: {m}" for k, m in h.raised]
        return stats
    finally:
        http.close()
        db.close()


def run_job(source_id: str, cfg: dict, limiter: DomainRateLimiter) -> None:
    """排程器呼叫的工作：任何例外都只記錄，不讓排程器停止。"""
    try:
        s = run_once(source_id, cfg=cfg, limiter=limiter)
        level = logging.WARNING if s["errors"] or s["alerts"] else logging.INFO
        log.log(level, "%s: listed=%d new=%d revised=%d errors=%d retries=%d alerts=%s",
                source_id, s["listed"], s["new_items"], s["revised"], s["errors"], s["retries"], s["alerts"])
    except Exception:
        log.exception("%s: job crashed", source_id)


def build_scheduler(cfg: dict, limiter: DomainRateLimiter | None = None):
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = cfg.get("defaults", {}).get("timezone", "Asia/Taipei")
    sources = implemented_sources(cfg)
    limiter = limiter or shared_limiter(cfg)
    sched = BlockingScheduler(timezone=tz, executors={"default": ThreadPoolExecutor(max(1, len(sources)))})
    for sid in sources:
        expr = source_config(cfg, sid)["schedule"]
        sched.add_job(run_job, CronTrigger.from_crontab(expr, timezone=tz), args=[sid, cfg, limiter],
                      id=sid, name=sid, max_instances=1, coalesce=True, misfire_grace_time=600)
    return sched


def serve(cfg: dict) -> None:
    sched = build_scheduler(cfg)
    for job in sched.get_jobs():
        log.info("scheduled %-12s %s", job.id, job.trigger)
    log.info("排程器啟動（Ctrl+C 結束）")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("排程器停止")


def print_events(db: Database) -> None:
    for e in events.pending(db, limit=5000):
        pl = e["payload"]
        tags = []
        if pl.get("unit"):
            tags.append(pl["unit"] + ("" if pl.get("in_scope", True) else "·僅列表"))
        if pl.get("is_insurance"):
            tags.append("保險業")
        if pl.get("feed"):
            tags.append(pl["feed"])
        if pl.get("pr_noise"):
            tags.append("公關稿")
        if pl.get("company"):
            tags.append(pl["company"])
        tag = f"[{'/'.join(tags)}] " if tags else ""
        print(f"#{e['id']:<5} {pl.get('source_id', ''):<12} {e['type']:<12} v{pl.get('version')} "
              f"{(pl.get('published_at') or '')[:10]}  {tag}{pl.get('title')}")


def _local(ts: str | None, tz: str) -> str:
    """資料庫存 UTC；顯示時轉成當地時間。"""
    if not ts:
        return "-"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def print_health(db: Database, sources: list[str], tz: str = "Asia/Taipei") -> None:
    for s in health.summary(db, sources):
        print(f"{s['source_id']:<12} 狀態 {s['last_status']:<8} 最後執行 {_local(s['last_run'], tz)}  "
              f"最後成功 {_local(s['last_success'], tz)}  上輪 listed={s['last_listed']} new={s['last_new']}")
        for a in s["open_alerts"]:
            print(f"    ⚠ {a['kind']} ×{a['occurrences']}：{a['message']}")


def print_products(db: Database) -> None:
    rows = db.conn.execute(
        """SELECT company, SUM(status='on_sale') AS on_sale, SUM(status='discontinued') AS disc,
                  SUM(latest_raw_doc_id IS NOT NULL) AS with_clause
           FROM products GROUP BY company ORDER BY company""").fetchall()
    for r in rows:
        print(f"{r['company']:<10} 銷售中 {r['on_sale']:>4}  已停售 {r['disc']:>3}  有條款 {r['with_clause']:>4}")
    for e in db.conn.execute(
            "SELECT type, payload, created_at FROM events WHERE type IN ('product_launched','product_discontinued')"
            " ORDER BY id DESC LIMIT 20"):
        pl = json.loads(e["payload"])
        mark = "＋上架" if e["type"] == "product_launched" else "－停售"
        print(f"  {mark} {e['created_at'][:10]} {pl.get('company')} {pl.get('title')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Insurance intel crawler")
    p.add_argument("source_id", nargs="?")
    p.add_argument("--refetch", action="store_true")
    p.add_argument("--config")
    p.add_argument("--all", action="store_true", help="所有已實作來源各跑一輪")
    p.add_argument("--serve", action="store_true", help="常駐排程")
    p.add_argument("--health", action="store_true", help="各來源狀態與警示")
    p.add_argument("--events", action="store_true", help="列出未處理事件")
    p.add_argument("--runs", action="store_true", help="列出最近 crawl_runs")
    p.add_argument("--products", action="store_true", help="各公司投資型商品數與最近上架／停售")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if (a.verbose or a.serve) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.serve:  # 常駐時不要每個 HTTP 請求都印一行
        logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(a.config)

    if a.events or a.runs or a.health or a.products:
        db = open_db(cfg)
        if a.products:
            print_products(db)
        if a.events:
            print_events(db)
        if a.runs:
            for r in db.conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 10"):
                print(dict(r))
        if a.health:
            print_health(db, implemented_sources(cfg), cfg.get("defaults", {}).get("timezone", "Asia/Taipei"))
        return 0

    if a.serve:
        serve(cfg)
        return 0

    if a.all:
        limiter = shared_limiter(cfg)
        failed = 0
        for sid in implemented_sources(cfg):
            s = run_once(sid, a.refetch, cfg=cfg, limiter=limiter)
            failed += bool(s["errors"] and s["listed"] == 0)
            extra = f" 上架={s['launched']} 停售={s['discontinued']}" if sid.startswith("company_") else ""
            print(f"{sid:<28} listed={s['listed']:<3} new={s['new_items']:<3} revised={s['revised']:<3} "
                  f"errors={s['errors']} retries={s['retries']}{extra}" + (f"  ⚠ {s['alerts']}" if s["alerts"] else ""))
        return 1 if failed else 0

    if not a.source_id:
        p.error("需要 source_id，或使用 --all / --serve / --health / --events / --runs")
    stats = run_once(a.source_id, a.refetch, cfg=cfg)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats["errors"] and stats["listed"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
