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

-- ======================= M4 解析層（Handbook 第三層；D22） =======================
-- 下游只訂閱 events；解析結果全部可由 raw 重建（刪掉這幾張表再跑 --parse 即可）。

-- 每個事件被解析層處理的紀錄：失敗會重試，超過 max_attempts 標 failed 並發警示（大聲失敗）。
CREATE TABLE IF NOT EXISTS event_processing (
    event_id      INTEGER PRIMARY KEY REFERENCES events(id),
    status        TEXT NOT NULL,          -- ok | skipped | error | failed
    handler       TEXT,                   -- clause | classify | product
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    processed_at  TEXT NOT NULL
);

-- 條款 PDF 結構化：每個條款版本（raw_doc）一筆
CREATE TABLE IF NOT EXISTS clause_docs (
    raw_doc_id      INTEGER PRIMARY KEY REFERENCES raw_docs(id),
    source_id       TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    company         TEXT,
    product_name    TEXT,
    version         INTEGER,
    pages           INTEGER,
    articles        INTEGER,               -- 切出的條文數
    fields          TEXT NOT NULL DEFAULT '{}',   -- JSON：統一 schema 欄位（規則抽取）
    evidence        TEXT NOT NULL DEFAULT '{}',   -- JSON：欄位 -> {article_no, title, page}，可追溯原文
    missing         TEXT NOT NULL DEFAULT '[]',   -- JSON：規則抽不到、留給 LLM 補的欄位
    warnings        TEXT NOT NULL DEFAULT '[]',
    parser          TEXT NOT NULL,          -- 例 clause-rules-v1
    parsed_at       TEXT NOT NULL
);

-- 條文（條號、標題、本文、頁碼）：條款逐條 diff 與問答引用的基本單位
CREATE TABLE IF NOT EXISTS clause_articles (
    raw_doc_id   INTEGER NOT NULL REFERENCES raw_docs(id),
    seq          INTEGER NOT NULL,          -- 0 = 條款前言（商品名稱、給付項目、文號）；附表為最後一段
    kind         TEXT NOT NULL,             -- preamble | article | appendix
    article_no   INTEGER,
    title        TEXT,
    text         TEXT NOT NULL,
    page_start   INTEGER,
    page_end     INTEGER,
    text_hash    TEXT NOT NULL,
    PRIMARY KEY (raw_doc_id, seq)
);

-- 條款改版（doc_revised）的逐條差異
CREATE TABLE IF NOT EXISTS clause_diffs (
    raw_doc_id           INTEGER PRIMARY KEY REFERENCES raw_docs(id),
    previous_raw_doc_id  INTEGER NOT NULL REFERENCES raw_docs(id),
    added                TEXT NOT NULL DEFAULT '[]',   -- JSON：新增的條號
    removed              TEXT NOT NULL DEFAULT '[]',
    changed              TEXT NOT NULL DEFAULT '[]',   -- JSON：[{article_no, title}]
    created_at           TEXT NOT NULL
);

-- 統一商品 schema（Handbook「功能清單」），每個商品一筆，指向最新條款版本
CREATE TABLE IF NOT EXISTS product_terms (
    company          TEXT NOT NULL,
    name             TEXT NOT NULL,
    line             TEXT,
    currency         TEXT,
    status           TEXT,                  -- on_sale | discontinued（同 products）
    issue_age        TEXT,
    payment_modes    TEXT,                  -- JSON list
    coverage         TEXT,                  -- JSON list：給付項目
    exclusions       TEXT,                  -- 除外責任條文摘要（前 200 字）
    rate_terms       TEXT,                  -- JSON：宣告利率／預定利率條文
    riders           TEXT,
    clause_raw_doc_id INTEGER REFERENCES raw_docs(id),
    clause_version   INTEGER,
    clause_url       TEXT,
    filings          TEXT,                  -- JSON：條款前言列出的備查／核准／修正文號
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (company, name)
);

-- 新聞與法規分類（商品／利率／法規／通路／人事＋影響程度）
CREATE TABLE IF NOT EXISTS doc_labels (
    raw_doc_id    INTEGER PRIMARY KEY REFERENCES raw_docs(id),
    source_id     TEXT NOT NULL,
    item_key      TEXT NOT NULL,
    title         TEXT,
    published_at  TEXT,
    categories    TEXT NOT NULL DEFAULT '[]',   -- JSON list
    impact        TEXT NOT NULL,                -- high | medium | low
    reasons       TEXT NOT NULL DEFAULT '[]',   -- JSON：命中的規則與關鍵字（可解釋）
    hidden        INTEGER NOT NULL DEFAULT 0,   -- 1 = 晨報預設隱藏（公關稿、非保險業）
    classifier    TEXT NOT NULL,                -- 例 rules-v1
    labeled_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_labels_impact ON doc_labels (impact, published_at DESC);
