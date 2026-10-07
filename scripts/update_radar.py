"""一鍵更新「商品情報雷達」網站的真實資料：爬取 → 解析 → 匯出 real-data.js →（可選）commit 並 push 讓 Vercel 重新部署。

    python scripts/update_radar.py                 # 全部來源跑一輪、解析、匯出到雷達網站資料夾（不 push）
    python scripts/update_radar.py --push          # 同上，有變動就 commit 並 push（排程用這個）
    python scripts/update_radar.py --skip-crawl    # 只重新解析與匯出（例如改了匯出規則）
    python scripts/update_radar.py --sources news_rss fsc_press   # 只跑部分來源

設定在 config/sources.yaml 的 radar 區塊（網站資料夾、要 push 的分支）。
定期自動執行：sh scripts/schedule_radar.sh install（macOS launchd，每天兩次）。

每一步失敗都不中斷後面的步驟：單一來源失敗只記錄，照樣用資料庫裡現有的資料匯出；
匯出失敗就不 commit，網站維持上一版資料。結果寫在 data/logs/last_update.json。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config, resolve                      # noqa: E402
from scheduler.run import implemented_sources, parse_once, run_once, shared_limiter   # noqa: E402

log = logging.getLogger("update_radar")
TPE = timezone(timedelta(hours=8))


def git(site: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(site), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}：{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def publish(site: Path, branch: str | None, snapshot: str) -> str:
    """只 commit real-data.js；網站資料夾有其他未提交的修改也不會被帶進去。"""
    cur = git(site, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and cur != branch:
        return f"略過 push：網站資料夾目前在 {cur} 分支，設定要 push 的是 {branch}"
    if not git(site, "status", "--porcelain", "--", "real-data.js"):
        return "real-data.js 沒有變動，不需要 commit"
    git(site, "add", "--", "real-data.js")
    git(site, "commit", "-m", f"資料自動更新：{snapshot}", "--", "real-data.js")
    git(site, "push", "origin", cur)
    return f"已 commit 並 push 到 {cur}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="有變動就 commit 並 push 網站的 real-data.js")
    ap.add_argument("--skip-crawl", action="store_true", help="不爬取，只解析與匯出")
    ap.add_argument("--sources", nargs="*", help="只跑這些來源（預設全部）")
    ap.add_argument("--site", help="雷達網站資料夾（預設讀 config 的 radar.site_dir）")
    a = ap.parse_args(argv)

    cfg = load_config()
    rcfg = cfg.get("radar") or {}
    site = Path(a.site or resolve(rcfg.get("site_dir", "../../paris-hackathon-business-competition"))).resolve()
    logs = resolve("data/logs")
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(logs / f"update_radar-{datetime.now(TPE):%Y%m}.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)

    lock = open(logs / "update_radar.lock", "w")
    try:   # 排程與手動同時跑時，後來的直接結束，避免兩個程序同時寫資料庫
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("另一個 update_radar 正在執行，這次略過")
        return 0

    started = time.time()
    report = {"started_at": datetime.now(TPE).isoformat(timespec="seconds"), "site": str(site), "sources": {}, "ok": True}
    if not (site / "index.html").exists():
        log.error("找不到雷達網站資料夾：%s（請設定 config/sources.yaml 的 radar.site_dir 或加 --site）", site)
        return 2

    # 1. 爬取：單一來源失敗不影響其他來源
    if not a.skip_crawl:
        limiter = shared_limiter(cfg)
        for sid in a.sources or implemented_sources(cfg):
            try:
                s = run_once(sid, cfg=cfg, limiter=limiter)
                report["sources"][sid] = {k: s[k] for k in ("listed", "new_items", "revised", "errors")} | {"alerts": s["alerts"]}
                log.info("%-28s listed=%d new=%d revised=%d errors=%d", sid, s["listed"], s["new_items"], s["revised"], s["errors"])
            except Exception as e:
                report["sources"][sid] = {"crashed": repr(e)}
                log.exception("%s 爬取失敗", sid)

    # 2. 解析：條款結構化、新聞／法規分類
    try:
        report["parse"] = parse_once(cfg)
        log.info("parse: %s", report["parse"])
    except Exception as e:
        report["parse"] = {"crashed": repr(e)}
        log.exception("解析失敗")

    # 3. 匯出 real-data.js（失敗就停在這裡，網站維持上一版資料）
    from scripts.export_radar import main as export_main
    try:
        export_main(["--out", str(site / "real-data.js")])
        X = json.loads((site / "real-data.js").read_text(encoding="utf-8").split("window.REAL_DATA = ", 1)[1].rstrip().rstrip(";"))
        report["export"] = X["meta"]
    except Exception as e:
        report["export"] = {"crashed": repr(e)}
        report["ok"] = False
        log.exception("匯出失敗，不更新網站")

    # 4. 發布
    if report["ok"] and a.push:
        try:
            report["publish"] = publish(site, rcfg.get("git_branch"), report["export"]["snapshot_at"])
            log.info(report["publish"])
        except Exception as e:
            report["publish"] = repr(e)
            report["ok"] = False
            log.exception("commit／push 失敗")

    report["seconds"] = round(time.time() - started)
    report["crashed_sources"] = [k for k, v in report["sources"].items() if "crashed" in v]
    (logs / "last_update.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("完成（%d 秒）：%s", report["seconds"], "成功" if report["ok"] else "有錯誤，見 data/logs/last_update.json")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
