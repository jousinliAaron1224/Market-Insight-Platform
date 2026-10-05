-- 處理層 SQLite schema（Handbook「儲存設計與資料表」）
-- 每筆都指回 raw 檔；處理層可由 raw 重建。正式版換 PostgreSQL 時欄位不變。
PRAGMA foreign_keys = ON;

-- 列表層去重
CREATE TABLE IF NOT EXISTS seen_items (
    source_id      TEXT NOT NULL,
    item_key       TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    list_fingerprint TEXT,      -- 列表上可見欄位（標題/日期/URL）的 hash；變了才重抓（決策 2026-10-02）
    PRIMARY KEY (source_id, item_key)
);

-- 每次抓取的版本紀錄（只記內容有變的版本）
CREATE TABLE IF NOT EXISTS raw_docs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id          TEXT NOT NULL,
    item_key           TEXT NOT NULL,
    version            INTEGER NOT NULL,
    content_hash       TEXT NOT NULL,
    raw_path           TEXT NOT NULL,
    fetched_at         TEXT NOT NULL,
    etag               TEXT,
    last_modified      TEXT,
    url                TEXT NOT NULL,
    doc_type           TEXT NOT NULL,
    meta               TEXT NOT NULL DEFAULT '{}',   -- JSON
    UNIQUE (source_id, item_key, version),
    UNIQUE (source_id, item_key, content_hash)       -- 冪等唯一鍵
);
CREATE INDEX IF NOT EXISTS ix_raw_docs_item ON raw_docs (source_id, item_key, version DESC);

-- 下游訂閱的事件流：new_item | doc_revised | product_launched | product_discontinued
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN
                  ('new_item','doc_revised','product_launched','product_discontinued')),
    raw_doc_id  INTEGER REFERENCES raw_docs(id),
    payload     TEXT NOT NULL DEFAULT '{}',          -- JSON
    created_at  TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_unprocessed ON events (processed, id);

-- 統一商品 schema（M3 起使用；完整欄位見 Handbook「功能清單」）
CREATE TABLE IF NOT EXISTS products (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    company            TEXT NOT NULL,
    name               TEXT NOT NULL,
    line               TEXT,              -- 險種
    currency           TEXT,
    status             TEXT NOT NULL DEFAULT 'on_sale' CHECK (status IN ('on_sale','discontinued')),
    latest_raw_doc_id  INTEGER REFERENCES raw_docs(id),
    updated_at         TEXT,
    UNIQUE (company, name)
);

-- 健康監控
CREATE TABLE IF NOT EXISTS crawl_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    items_listed   INTEGER NOT NULL DEFAULT 0,
    items_new      INTEGER NOT NULL DEFAULT 0,       -- 寫入新版本（new_item + doc_revised）
    items_not_modified INTEGER NOT NULL DEFAULT 0,   -- HTTP 304
    items_unchanged    INTEGER NOT NULL DEFAULT 0,   -- content_hash 相同而丟棄
    errors         INTEGER NOT NULL DEFAULT 0,
    error_detail   TEXT
);
CREATE INDEX IF NOT EXISTS ix_crawl_runs_source ON crawl_runs (source_id, started_at DESC);

-- 新聞 RSS 導言暫存（決策 D16）：Handbook 規定新聞只長期保存標題、連結、時間與自產摘要，
-- 媒體導言僅供分類／摘要處理，保存 lead_ttl_days（預設 30 天）後清除；raw 層不含導言。
CREATE TABLE IF NOT EXISTS news_leads (
    source_id   TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    raw_doc_id  INTEGER REFERENCES raw_docs(id),
    lead_text   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_news_leads_expires ON news_leads (expires_at);

-- 健康檢查警示（Handbook「可靠性與監控」）：同一來源同一種問題只保留一筆未解除的警示，
-- 重複發生時累加 occurrences；狀況恢復時自動填 resolved_at。
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,       -- zero_listed | missing_fields | run_errors
    message        TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    occurrences    INTEGER NOT NULL DEFAULT 1,
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts (source_id, kind, resolved_at);
