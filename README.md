# insurance-intel

保險商品與市場情報平台：資料來源層（BNP Paribas Cardif 黑客松 Theme 3-1）。
設計依據是《保險商品與市場情報平台 Handbook》，決策一律以 Handbook 為準。

## 快速開始

```bash
cd insurance-intel
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                  # 67 個測試，全部離線（M3 用實際下載的官網 PDF，約 50 秒）

python -m scheduler.run tii_law_rss -v     # 真的抓一輪（約 3 分鐘：50 筆內文，每筆間隔 3 秒）
python -m scheduler.run --events           # 應該看到 50 筆 new_item
python -m scheduler.run tii_law_rss        # 再跑一次：skipped_seen=50、new=0
python -m scheduler.run --runs             # crawl_runs 健康紀錄

python -m scheduler.run fsc_press -v      # 金管會新聞稿（約 1–2 分鐘）
python -m scheduler.run fsc_penalty -v    # 金管會裁罰案件（1 個請求）
python -m scheduler.run news_rss -v       # 中央社財經（工商時報暫停，見 D17）

python -m scheduler.run --all             # 所有來源各跑一輪
python -m scheduler.run --health          # 各來源最後成功時間、狀態與未解除警示
python -m scheduler.run --serve           # 常駐排程，依 sources.yaml 的 cron 自動跑（Ctrl+C 結束）
python -m scheduler.run --products        # 各公司投資型商品數、最近上架／停售
```

### 競品商品資料（M3）

法巴人壽（自家基準）、國泰、富邦、台灣人壽、凱基各一個 adapter（`adapters/companies/`），範圍是投資型。

- **商品清單**：各公司依《人身保險業辦理資訊公開管理辦法》公開的「保險商品名稱、日期及文號」PDF。名稱含「變額／投資型」的主約才收，批註條款與附約不收。名稱和文號常跨列，所以用「有厚度的分隔線」切出商品區塊（`disclosure_pdf.py`）。
- **上架／停售**：每輪比對前後兩次商品集合，新出現發 `product_launched`、消失發 `product_discontinued`，同步更新 `products` 表。第一次執行只建立基準，不發上架事件。若本輪商品數少於上次的一半，視為網站改版或解析失敗，不判定停售，改發警示。
- **條款**：從各家「契約條款」頁（台灣人壽是 PDF 超連結）對應商品名稱 → 條款 PDF，原檔存 raw。條款內容改變時保留舊版、新增版本並發 `doc_revised`。找不到條款的商品只記清單那一列，標 `parse_warnings`。
- 保發中心「保險商品查詢」資料庫雖然收錄全市場，但查詢有圖形驗證碼，不自動化。
- 台灣人壽的清單是固定檔案編號（`portal-api/File/10904`），頁面 API 被防火牆擋，無法自動找新版；官網換檔案時要手動更新 `sources.yaml`。

### 排程與可靠性（M2）

- `--serve` 用 APScheduler 依 `sources.yaml` 的 cron（台北時間）排程。同一來源不會重疊執行；排程延誤 10 分鐘內會補跑一次，超過就跳過、等下一個排程時間。
- 所有來源共用一個限速器，同一網域同時只會有 1 個請求，間隔至少 3 秒，跨來源也一樣（例如兩個金管會來源）。
- 網路錯誤、429、5xx 依 30／120／600 秒重試；403、404 與解析錯誤不重試，直接記錄並警示。
- 每輪結束會做健康檢查，結果寫入 `alerts` 表並印出 WARNING：
  - 連續 3 輪 0 筆
  - 必要欄位解析失敗超過 30%
  - 本輪有錯誤

  狀況恢復時警示會自動解除。中央社這種本來就常 0 筆的來源設 `allow_empty_runs: true`。
- 排程器要在電腦開著、終端機開著時才會跑；Mac 睡眠期間錯過的排程會跳過，醒來後照下一個排程時間繼續。

需要 Python 3.10 以上。資料庫位置 `data/intel.db`，原始檔放在 `data/raw/<source_id>/<yyyy-mm-dd>/<sha256>.<ext>`。

### TLS：law.tii.org.tw 憑證驗證失敗時

law.tii.org.tw 的憑證鏈在 Python 下會出現 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`。瀏覽器會自動補抓缺少的中繼憑證，Python 不會；另一種可能是簽發的根憑證不在 Mozilla 清單內。處理方式是把簽發者憑證加進這個來源的信任清單，**不關閉驗證**：

```bash
sh scripts/fetch_issuer_cert.sh law.tii.org.tw   # 產生 config/certs/law.tii.org.tw.pem
```

`config/sources.yaml` 的 `extra_ca_files` 已經指向這個檔案。

## 結構

| 路徑 | 內容 |
|---|---|
| `adapters/base.py` | `SourceAdapter`、`ItemRef`、`RawDoc`、`NotModified` |
| `adapters/tii_law_rss.py` | 保發中心法規動態：RSS 加上 ShowNews 內文頁 |
| `adapters/fsc_press.py` | 金管會新聞稿：列表頁加上指定單位的內文頁 |
| `adapters/fsc_penalty.py` | 金管會裁罰案件 RSS：發文字號、受處分人、罰鍰金額、是否保險業 |
| `adapters/news_rss.py` | 工商時報保險脈動＋中央社財經 RSS：關鍵字過濾、公關稿標記、導言暫存 |
| `adapters/companies/` | M3：商品清單 PDF 解析、條款對照、5 家公司 adapter |
| `adapters/common.py` | 時區、正文指紋、HTML 轉純文字 |
| `core/schema.sql` | SQLite schema：seen_items、raw_docs、events、products、crawl_runs |
| `core/storage.py` | `RawStore`（依內容雜湊命名、不可變）與 `Database` |
| `core/change_detect.py` | 三層變動偵測 `run_source()` |
| `core/events.py` | `emit` / `pending` / `mark_processed` |
| `core/http.py`、`core/ratelimit.py` | UA、每網域限速與並發上限、重試退避、robots、conditional GET |
| `config/sources.yaml` | 所有來源設定 |
| `scheduler/run.py` | CLI：單一來源、`--all`、`--serve`（APScheduler）、`--health` |
| `core/health.py` | 健康檢查與警示（alerts 表） |

## M1 決策紀錄（2026-10-02，Handbook 未定義、經 Chris 確認）

1. **tii_law_rss 的來源**：使用 `law.tii.org.tw/Fn/rss.asp` 與內文頁 `ShowNews.asp`。該網域的 robots.txt 是 `Disallow: /`，所以這個來源設定 `respect_robots: false`，並以 1 並發、間隔至少 3 秒的方式低頻抓取。data.gov.tw 44724 的開放資料 API（openapi.tii.org.tw K101）落後約 2 個月，而且沒有內文。（已寫入 Handbook 決策紀錄 D1。）
2. **fetch 的範圍**：RSS item 存成 `.json` 快照，內文頁存原始 Big5 `.html`；`RawDoc.content_hash` 以內文頁為準。
3. **公告改版**：同一個 item_key 內容變了，寫入 version+1 並發 `doc_revised`。
4. **已看過的項目何時重新檢查**：`seen_items.list_fingerprint`（標題／日期／URL 的 hash）改變時，或手動加 `--refetch` 時。
5. **HTTP 304 層**：照通用邏輯實作。TII 不回 ETag/Last-Modified，所以這層對它不會作用。
6. **User-Agent 聯絡人**：jousinli1224@gmail.com。

## M2 決策紀錄（2026-10-02）

完整內容見 Handbook「決策紀錄」D11–D14。

- D11 content_hash：預設是原始位元組的 sha256；頁面有雜訊（例如瀏覽人次）時，adapter 改用正文指紋。raw 仍存原檔，因雜訊產生、沒被任何紀錄引用的 raw 檔會刪除
- D12 fsc_press：用列表頁加內文頁；只抓保險局、金管會本部、檢查局的內文，其他單位只記列表列（`doc_type=list_row`）
- D13 事件 payload：會帶上 adapter 的 `meta.event_extra`（例如 `unit`、`in_scope`、`is_insurance`），下游用來過濾
- D14 新增 fsc_penalty（裁罰案件 RSS），全部收錄，以發文字號「金管保」判斷是否為保險業案件
- D15 TLS 嚴格模式：www.fsc.gov.tw 的憑證缺 Subject Key Identifier，Python 3.13+ 預設的 X.509 嚴格檢查會拒絕。兩個金管會來源設 `x509_strict: false`，只關這項格式檢查，憑證鏈與主機名稱照常驗證
- D16 news_rss 只用 RSS 導言、不抓新聞內文頁。raw 只存標題、連結、時間；導言放 `news_leads` 暫存表，30 天後於每輪開始時清除
- D17 工商時報 RSS 前有 Cloudflare 機器人防護，程式請求一律 403，不以偽裝瀏覽器繞過，`enabled: false` 暫停；目前 news_rss 只有中央社
- D18 排程以 Asia/Taipei 解讀 cron；健康檢查警示存 `alerts` 表（同類未解除只留一筆並累加次數，恢復自動解除）；只重試暫時性錯誤（網路、429、5xx）
- D19 M3 範圍：法巴＋國泰、富邦、台灣人壽、凱基的投資型（名稱含變額／投資型的主約）；清單來源為法定公開的商品名稱文號 PDF
- D20 上架／停售：商品集合差異；首輪只建基準；清單驟降超過一半不判停售改發警示
- D21 ItemRef.published_at 用最近一次核准／修正日期，商品修正時自動重抓條款；條款 PDF 改變發 doc_revised

## 來源特性備忘（tii_law_rss）

- `pubDate` 是民國年 `yyyMMdd`（例：`1150930`），adapter 自行轉換成 Asia/Taipei 時間
- feed 內的 link 是 `http://`，正規化成 `https://`
- 內文頁是 Big5；解析不到欄位時寫入 `meta.parse_warnings`，不會丟例外（供健康檢查使用）
- `tests/fixtures/tii_law_rss/rss.xml` 是用 2026-10-02 實際取樣的 50 筆依原格式重組；`shownews_3867.html` 是節錄版

## 已知限制

- 內容改回舊版（A→B→A）時，因為 `item_key + content_hash` 是唯一鍵，不會另存新版本，只記為未變動
