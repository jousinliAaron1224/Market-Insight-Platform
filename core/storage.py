"""儲存層：raw 不可變檔案庫 + SQLite 處理層（Handbook「儲存設計與資料表」）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore:
    """data/raw/<source_id>/<yyyy-mm-dd>/<content_hash>.<ext>，原封不動、永不修改。

    以內容雜湊命名，相同內容只會落地一次（冪等）。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, source_id: str, data: bytes, ext: str, fetched_at: datetime) -> tuple[str, str]:
        """寫入原始位元組，回傳 (raw_path, content_hash)。raw_path 相對於 root 的上層目錄。"""
        digest = sha256(data)
        existing = self.find(source_id, digest, ext)
        if existing is not None:
            return existing, digest
        day = fetched_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        path = self.root / source_id / day / f"{digest}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # 原子寫入，避免半個檔
        return self._rel(path), digest

    def find(self, source_id: str, digest: str, ext: str) -> str | None:
        for p in (self.root / source_id).glob(f"*/{digest}.{ext}"):
            return self._rel(p)
        return None

    def discard_orphan(self, raw_path: str, db: "Database") -> bool:
        """刪除「剛寫入、但變動偵測判定為未變動且沒有任何紀錄引用」的 raw 檔。

        只在內容指紋與原始位元組不同（D11）時會發生，例如頁面上的瀏覽人次變了。
        已被 raw_docs 引用的檔案永不刪除，維持 raw 層不可變。
        """
        if db.conn.execute("SELECT 1 FROM raw_docs WHERE raw_path=? LIMIT 1", (raw_path,)).fetchone():
            return False
        path = self.root.parent / raw_path
        if path.is_file():
            path.unlink()
            return True
        return False

    def read(self, raw_path: str) -> bytes:
        return (self.root.parent / raw_path).read_bytes()

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root.parent).as_posix()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)  # 排程器多執行緒寫入時等待鎖
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- seen_items（列表層） ----
    def seen_fingerprint(self, source_id: str, item_key: str) -> str | None:
        """未看過回傳 None；看過回傳上次的列表指紋（可能為空字串）。"""
        row = self.conn.execute(
            "SELECT list_fingerprint FROM seen_items WHERE source_id=? AND item_key=?",
            (source_id, item_key),
        ).fetchone()
        return None if row is None else (row[0] or "")

    def touch_seen(self, source_id: str, item_key: str, now: datetime,
                   fingerprint: str | None = None) -> None:
        ts = iso(now)
        self.conn.execute(
            """INSERT INTO seen_items (source_id, item_key, first_seen_at, last_seen_at, list_fingerprint)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source_id, item_key) DO UPDATE SET
                   last_seen_at=excluded.last_seen_at,
                   list_fingerprint=COALESCE(excluded.list_fingerprint, seen_items.list_fingerprint)""",
            (source_id, item_key, ts, ts, fingerprint),
        )

    # ---- raw_docs（版本紀錄） ----
    def latest_version(self, source_id: str, item_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM raw_docs WHERE source_id=? AND item_key=?
               ORDER BY version DESC LIMIT 1""",
            (source_id, item_key),
        ).fetchone()

    def find_by_hash(self, source_id: str, item_key: str, content_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM raw_docs WHERE source_id=? AND item_key=? AND content_hash=?",
            (source_id, item_key, content_hash),
        ).fetchone()

    def insert_raw_doc(self, doc: Any, version: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path,
                                     fetched_at, etag, last_modified, url, doc_type, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.source_id, doc.item_key, version, doc.content_hash, doc.raw_path,
                iso(doc.fetched_at), doc.http_etag, doc.http_last_modified, doc.url,
                doc.doc_type, json.dumps(doc.meta, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)

    # ---- news_leads（暫存，D16） ----
    def put_lead(self, source_id: str, item_key: str, raw_doc_id: int | None, text: str,
                 now: datetime, ttl_days: int) -> None:
        from datetime import timedelta
        self.conn.execute(
            """INSERT INTO news_leads (source_id, item_key, raw_doc_id, lead_text, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, item_key) DO UPDATE SET
                   raw_doc_id=excluded.raw_doc_id, lead_text=excluded.lead_text,
                   created_at=excluded.created_at, expires_at=excluded.expires_at""",
            (source_id, item_key, raw_doc_id, text, iso(now), iso(now + timedelta(days=ttl_days))),
        )

    def get_lead(self, source_id: str, item_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT lead_text FROM news_leads WHERE source_id=? AND item_key=?", (source_id, item_key)
        ).fetchone()
        return row[0] if row else None

    def purge_expired_leads(self, now: datetime) -> int:
        cur = self.conn.execute("DELETE FROM news_leads WHERE expires_at < ?", (iso(now),))
        self.conn.commit()
        return cur.rowcount

    # ---- crawl_runs（健康監控） ----
    def start_run(self, source_id: str, now: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (source_id, started_at) VALUES (?, ?)", (source_id, iso(now))
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, int], status: str,
                   error_detail: str | None, now: datetime) -> None:
        self.conn.execute(
            """UPDATE crawl_runs SET finished_at=?, status=?, items_listed=?, items_new=?,
                   items_not_modified=?, items_unchanged=?, errors=?, error_detail=?
               WHERE id=?""",
            (iso(now), status, stats.get("listed", 0), stats.get("new", 0),
             stats.get("not_modified", 0), stats.get("unchanged", 0), stats.get("errors", 0),
             error_detail, run_id),
        )
        self.conn.commit()
