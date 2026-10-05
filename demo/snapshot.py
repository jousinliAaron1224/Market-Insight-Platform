"""Demo 資料快照（M4，D24）：把目前的資料庫與它引用到的 raw 檔複製成一個自足資料夾。

Handbook「Demo 策略」：現場不即時爬網站，賽前抓好快照，現場用重播腳本展示完整流程。

    python -m demo.snapshot create demo-1004            # data/snapshots/demo-1004/
    python -m demo.snapshot create demo-1004 --zip      # 另外壓成 data/snapshots/demo-1004.zip，可帶到別台電腦
    python -m demo.snapshot verify demo-1004            # 逐檔比對雜湊，確認快照完整
    python -m demo.snapshot list

快照內容：
    intel.db        SQLite 線上備份（WAL 模式下也一致），news_leads 預設清空（D16：導言只暫存 30 天）
    raw/...         raw_docs 與 meta 中所有 *_path 引用到的原始檔，路徑與 data/raw 相同
    manifest.json   建立時間、來源、各表筆數、每個檔案的 sha256 與大小

raw 檔名本來就是內容的 sha256（RawStore），verify 會同時檢查「檔名＝內容雜湊」與 manifest。
快照建立後不再修改；重播時會另外複製一份工作用資料庫，不動快照本身。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from core.config import load_config, resolve
from core.storage import iso, utcnow

SNAP_DIRNAME = "snapshots"


def snapshots_root(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return resolve(cfg["defaults"]["db_path"]).parent / SNAP_DIRNAME


def resolve_snapshot(name_or_path: str, cfg: dict | None = None) -> Path:
    """快照名稱（data/snapshots/<名稱>）或資料夾路徑（例如解壓到別處的快照）。"""
    by_name = snapshots_root(cfg) / name_or_path
    if (by_name / "manifest.json").is_file():
        return by_name
    p = Path(name_or_path)
    if (p / "manifest.json").is_file():
        return p
    raise FileNotFoundError(f"找不到快照：{name_or_path}（python -m demo.snapshot list 可列出現有快照）")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def referenced_raw_paths(conn: sqlite3.Connection) -> list[str]:
    """raw_docs.raw_path 以及 meta 裡所有以 _path 結尾的欄位（RSS item 快照、商品清單 PDF…）。"""
    paths: list[str] = []
    for raw_path, meta in conn.execute("SELECT raw_path, meta FROM raw_docs ORDER BY id"):
        paths.append(raw_path)
        for k, v in (json.loads(meta or "{}")).items():
            if k.endswith("_path") and isinstance(v, str) and v.startswith("raw/"):
                paths.append(v)
    return list(dict.fromkeys(paths))


def table_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    out["events_by_type"] = dict(conn.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall())
    out["raw_docs_by_source"] = dict(conn.execute(
        "SELECT source_id, COUNT(*) FROM raw_docs GROUP BY source_id").fetchall())
    return out


def create(name: str, cfg: dict | None = None, *, keep_leads: bool = False, make_zip: bool = False,
           overwrite: bool = False) -> Path:
    cfg = cfg or load_config()
    db_path = resolve(cfg["defaults"]["db_path"])
    data_dir = resolve(cfg["defaults"]["raw_root"]).parent       # raw_path 相對於 data/
    dest = snapshots_root(cfg) / name
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"快照已存在：{dest}（快照建立後不修改；換個名稱或加 --overwrite）")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    snap_db = dest / "intel.db"
    dst = sqlite3.connect(snap_db)
    src.backup(dst)                                   # 線上備份：WAL 中尚未 checkpoint 的寫入也會帶到
    src.close()
    has_leads = dst.execute("SELECT 1 FROM sqlite_master WHERE name='news_leads'").fetchone()
    if has_leads and not keep_leads:
        dst.execute("DELETE FROM news_leads")
    dst.commit()
    dst.execute("PRAGMA journal_mode=DELETE")         # 單一檔案，方便壓縮帶走
    dst.execute("VACUUM")

    files, missing = [], []
    for rel in referenced_raw_paths(dst):
        s = data_dir / rel
        if not s.is_file():
            missing.append(rel)
            continue
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        files.append({"path": rel, "sha256": _sha256_file(d), "bytes": d.stat().st_size})
    manifest = {
        "name": name,
        "created_at": iso(utcnow()),
        "source_db": str(db_path),
        "news_leads_kept": keep_leads,
        "counts": table_counts(dst),
        "db_sha256": None,
        "files": files,
        "missing_raw": missing,
    }
    dst.close()
    manifest["db_sha256"] = _sha256_file(snap_db)
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if make_zip:
        shutil.make_archive(str(dest), "zip", root_dir=dest.parent, base_dir=name)
    return dest


def verify(name_or_path: str, cfg: dict | None = None) -> dict[str, Any]:
    dest = resolve_snapshot(name_or_path, cfg)
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    if _sha256_file(dest / "intel.db") != m["db_sha256"]:
        problems.append("intel.db 與 manifest 不符（快照被改過？）")
    for f in m["files"]:
        fp = dest / f["path"]
        if not fp.is_file():
            problems.append(f"缺檔：{f['path']}")
            continue
        h = _sha256_file(fp)
        if h != f["sha256"]:
            problems.append(f"雜湊不符：{f['path']}")
        elif Path(f["path"]).stem != h:
            problems.append(f"檔名不是內容雜湊：{f['path']}")
    conn = sqlite3.connect(f"file:{dest / 'intel.db'}?mode=ro", uri=True)
    listed = {f["path"] for f in m["files"]}
    for rel in referenced_raw_paths(conn):
        if rel not in listed and rel not in m.get("missing_raw", []):
            problems.append(f"資料庫引用但 manifest 沒有：{rel}")
    conn.close()
    return {"name": m["name"], "files": len(m["files"]), "bytes": sum(f["bytes"] for f in m["files"]),
            "missing_raw_at_create": m.get("missing_raw", []), "problems": problems, "ok": not problems}


def list_snapshots(cfg: dict | None = None) -> list[dict[str, Any]]:
    root = snapshots_root(cfg)
    out = []
    for d in sorted(root.glob("*/manifest.json")) if root.exists() else []:
        m = json.loads(d.read_text(encoding="utf-8"))
        out.append({"name": m["name"], "created_at": m["created_at"], "files": len(m["files"]),
                    "events": m["counts"].get("events"), "bytes": sum(f["bytes"] for f in m["files"])})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Demo 資料快照")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--zip", action="store_true", help="另外壓成 .zip")
    c.add_argument("--keep-leads", action="store_true", help="保留新聞導言暫存（預設清空，D16）")
    c.add_argument("--overwrite", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("name")
    sub.add_parser("list")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "create":
        dest = create(a.name, cfg, keep_leads=a.keep_leads, make_zip=a.zip, overwrite=a.overwrite)
        r = verify(str(dest), cfg)
        m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        print(f"快照 {dest}")
        print(f"  事件 {m['counts'].get('events')}（{m['counts']['events_by_type']}）")
        print(f"  raw 檔 {r['files']} 個，{r['bytes'] / 1e6:.1f} MB；驗證 {'OK' if r['ok'] else '有問題'}")
        if r["missing_raw_at_create"]:
            print(f"  ⚠ 資料庫引用但 data/raw 找不到：{len(r['missing_raw_at_create'])} 個")
        return 0 if r["ok"] else 1
    if a.cmd == "verify":
        r = verify(a.name, cfg)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    for s in list_snapshots(cfg):
        print(f"{s['name']:<20} {s['created_at']}  事件 {s['events']}  raw {s['files']} 個 {s['bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
