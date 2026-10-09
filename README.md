# insurance-intel

保險商品與市場情報平台：資料來源層（BNP Paribas Cardif 黑客松 Theme 3-1）。
設計依據是《保險商品與市場情報平台 Handbook》，決策一律以 Handbook 為準。

## 快速開始

```bash
cd insurance-intel
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                  # 120 個測試，全部離線（M3／M4 用實際下載的官網 PDF，約 75 秒）

python -m scheduler.run tii_law_rss -v     # 真的抓一輪（約 3 分鐘：50 筆內文，每筆間隔 3 秒）
python -m scheduler.run --events           # 應該看到 50 筆 new_item
python -m scheduler.run tii_law_rss        # 再跑一次：skipped_seen=50、new=0
python -m scheduler.run --runs             # crawl_runs 健康紀錄

python -m scheduler.run fsc_press -v      # 金管會新聞稿（約 1–2 分鐘）
python -m scheduler.run fsc_penalty -v    # 金管會裁罰案件（1 個請求）
python -m scheduler.run news_rss -v       # 中央社財經＋自由時報財經（D26；工商時報暫停，見 D17）

python -m scheduler.run --all             # 所有來源各跑一輪
python -m scheduler.run --health          # 各來源最後成功時間、狀態與未解除警示
python -m scheduler.run --serve           # 常駐排程，依 sources.yaml 的 cron 自動跑（Ctrl+C 結束）
python -m scheduler.run --products        # 各公司投資型商品數、最近上架／停售
python -m scheduler.run open_data -v      # 政府開放資料：保費收入依險種（I10）、壽險財務業務指標（7191）、壽險業績統計（D28，市場數據用）
python scripts/probe_open_data.py 7191    # 先看一眼某個開放資料集的檔案與欄位（不存檔）

python -m scheduler.run --parse -v        # M4 解析層：消化未處理事件（條款結構化、新聞／法規分類）
python -m scheduler.run --terms           # 統一商品 schema：各公司已解析條款與欄位覆蓋
python -m scheduler.run --labels          # 分類結果，依影響程度排序

python -m demo.snapshot create demo-1004 --zip   # demo 快照（先跑 --parse）
python -m demo.replay demo-1004 --delay 0.8      # 從快照重播過去 90 天
```

### 前端原型

```bash
python -m web.server                          # 讀 data/intel.db（先跑 --parse），開 http://127.0.0.1:8765
python -m web.server --snapshot demo-1005b    # 讀重播後的工作資料庫，原文從快照讀
```

只用 Python 標準庫，唯讀讀 SQLite，只綁本機位址。純 HTML＋原生 JavaScript（`web/static/`），不做設計，只求清楚能用。

- **商品工作台**（`/`，D30）：從產品出發的模組化商品開發。投資型商品拆成 9 個模組（商品定位、投保規則、保障設計、投資標的、費用結構、保單權益、除外責任與附約、銷售與合規、送審；定義在 `config/product_modules.yaml`）。可空白開始或從競品複製（自動帶入從條款與附表擷取的欄位：險種、幣別、繳費方式、身故給付型態、年金保證期間、全委帳戶、資產撥回、保費費用率、解約費用率…）。左邊逐個模組填寫（自動儲存在 `data/drafts.db`，與唯讀的情報資料庫分開），右邊四個分頁跟著模組切換：**競品**（同險種的欄位分布與自家數量、挑一個競品看相關條文與附表原文）、**法規**（主要法規清單＋法規動態標題比對）、**新聞**、**市場**（各家險種組成、外幣占比、保費成長、費用率、繼續率、最近核准商品）。每個項目可「＋ 釘選」到規格書；檢查提示（高齡投保、死亡給付最低比率、外幣、撥回、通路、費用高於／低於全部競品、未填必填欄位）只提示要確認並附法規出處。`/spec.html?id=` 是可列印的商品規格書（設定值＋同險種競品分布＋法規＋釘選參考＋檢查事項）。
- **情報牆**（`/wall.html`）：最上面是「本週要注意」（以資料最新日期往回 7 天，可切 14／30 天）：高影響、或會影響法巴自家商品的法規與新聞，以及期間內的商品上架／停售／改版。下面分「法規動態」（保發中心、金管會新聞稿、裁罰）與「新聞動態」兩個分頁，可依影響程度／類別／來源／日期／標題篩選，可只看影響自家商品的。每筆的「對現有商品的影響」與「細節」列出判定理由、自家受影響商品（依險種統計、點了到競品比較）、各競品受影響數、建議檢視條文（D27）。
- **競品比較**（`/compare.html`）：篩選商品、勾選最多 4 個並排比較統一欄位，每個欄位附出處（條號、標題、頁碼），點了開原文 PDF 那一頁；「條文對照」依條文標題並排各家原文，可輸入關鍵字（例如「解約費用」）找出含該字的條文。比較結果的網址可以存起來或分享。
- **市場數據**（`/market.html`）：最上面是可引用的結論句（附來源）。供給面用 M3 的法定公開商品清單算各家每季新核准商品數（SVG 圖）、各家銷售中商品／系列數、外幣與險種結構、近 12 個月新核准與修正、條款修正集中的月份與同一天的金管會文號（辨識主管機關要求的全面修正）、最近 180 天新核准商品。需求面用保發中心「人身保險業保費收入」（104113／I10：全市場依險種、依月；含續期保費、不分投資型）算今年累計 vs 去年同期、各險種成長與占比、最近 10 年；壽險公會 14539 只有 2020 年公告的一期投資型數字，列為參考。公司比較用保險局「壽險財務業務指標」（7191：各公司每季比率，欄位 AMOUNT1…23 依資料集說明順序對應）畫自家與競品的保費收入變動率、初年度保費比率、繼續率、新契約費用率、ROE，對照全部壽險公司中位數與排名。市占率需要各公司保費金額，開放資料目前沒有（13517 只有產／壽險業合計），來源待確認（`scripts/probe_open_data.py`）。

### 交給下游（M4）

決策見 Handbook D22–D24。

- **解析層**（`parsers/`）：`pipeline.process_pending()` 依序消化 `events`，交給 handler：
  - `clause`：company_* 的條款 PDF → `clause_docs`（統一 schema 欄位、出處、待補欄位）、`clause_articles`（條號／標題／本文／頁碼）、`product_terms`；doc_revised 另寫 `clause_diffs`（逐條差異）
  - `classify`：保發中心、金管會、裁罰、新聞 → `doc_labels`（商品／利率／法規／通路／人事＋高／中／低、可解釋的命中原因、預設隱藏）
  - `product`：product_launched／discontinued → `product_terms.status`
  - 失敗會重試，同一事件 3 次失敗標 failed 並發 `parser` 警示；`--serve` 每 10 分鐘自動跑一次
- **純規則、LLM 介面預留**：`parsers.base.Enricher`。sources.yaml 的 `parsing.llm` 指定實作後，只能補 `missing` 內的欄位且必須附出處條號。
- **條款解析**（`parsers/clause_pdf.py`）：法巴雙欄自動偵測；去頁首頁尾；條號必須連續（避免把行首的「第十九條約定…」當新條文）；欄位都記條號與頁碼。投保年齡不在條款（在要保規則），目前一律列為待補。
- **分類規則**在 sources.yaml 的 `parsing.classify`，關鍵字是正規表示式（「上半年金融」不算年金、「非投資型」不算投資型）。
- **快照**（`demo/snapshot.py`）：`data/snapshots/<名稱>/` 含資料庫線上備份、引用到的 raw 檔與 manifest（sha256）；不可覆蓋，`verify` 檢查完整性；新聞導言預設不帶（D16）。
- **重播**（`demo/replay.py`）：在 `data/replay/<快照>/` 的工作副本上，依真實日期重建過去 N 天：文字來源依發布日；商品依清單 PDF 的首次核准日重建上架、依最近修正日重建條款改版（標 `reconstructed`）。查結果：`python -m scheduler.run --db data/replay/<快照>/intel.db --labels`。
- 條款對應防呆：條款前言找不到商品名稱、或抬頭是批註條款時，標「條款與商品不符／非主約條款」並不採用其欄位。
- 來源更正（D25）：同一項目改抓另一個網址且內容不同時，仍存新版本並發 `doc_revised`，但帶 `url_changed`／`previous_url`；解析層視為來源更正，不做逐條比對、不當改版顯示。
- 2026-10-05 修正台灣人壽條款對應（`clause_index.from_pdf_hyperlinks`）：條款清單列距約 13.5pt，舊版把上下相鄰列的名稱併進來，導致 40 個商品全部對到隔壁列的條款。修正後下次抓取會自動改抓正確檔案（列表指紋含網址），產生 40 筆標 `url_changed` 的來源更正。

### 競品商品資料（M3）

法巴人壽（自家基準）、國泰、富邦、台灣人壽、凱基各一個 adapter（`adapters/companies/`），範圍是投資型。

- **商品清單**：各公司依《人身保險業辦理資訊公開管理辦法》公開的「保險商品名稱、日期及文號」PDF。名稱含「變額／投資型」的主約才收，批註條款與附約不收。名稱和文號常跨列，所以用「有厚度的分隔線」切出商品區塊（`disclosure_pdf.py`）。
- **上架／停售**：每輪比對前後兩次商品集合，新出現發 `product_launched`、消失發 `product_discontinued`，同步更新 `products` 表。第一次執行只建立基準，不發上架事件。若本輪商品數少於上次的一半，視為網站改版或解析失敗，不判定停售，改發警示。
- **條款**：從各家「契約條款」頁（台灣人壽是 PDF 超連結）對應商品名稱 → 條款 PDF，原檔存 raw。條款內容改變時保留舊版、新增版本並發 `doc_revised`。找不到條款的商品只記清單那一列，標 `parse_warnings`。
- 保發中心「保險商品查詢」資料庫雖然收錄全市場，但查詢有圖形驗證碼，不自動化。
- 台灣人壽的清單是固定檔案編號（`portal-api/File/10904`），頁面 API 被防火牆擋，無法自動找新版；官網換檔案時要手動更新 `sources.yaml`。

### 法規草案預告、銀行上架、宣告利率（2026-10-05 新增）

```bash
python -m scheduler.run fsc_draft -v        # 金管會法規草案預告 RSS（1 個請求）
python -m scheduler.run bank_shelf -v       # 兆豐、華南、永豐的保險商品列表（7 個請求）
python -m scheduler.run declared_rates -v   # 宣告利率；第一次回補 12 個月約 600 個請求、40 分鐘，之後每月幾十個
```

- **fsc_draft**（`adapters/fsc_draft.py`）：`/RSS/Noticelaw?serno=201202290010`（同一個 serno 用 `/RSS/Messages` 會回傳空 channel）。最近 20 筆，description 是完整公告；以 category cake=490、字號「金管保」或承辦單位「保險局」判斷 `is_insurance`，非保險局的草案由解析層預設隱藏（同裁罰案）。陳述意見期限從「刊登公報翌日」起算、RSS 沒有刊登日，所以只記 `comment_days`（「三十日內」這類國字也會轉換）；標題有「預告期間：起~迄」時才記 `comment_end`，不自行推算。匯出到雷達網站的法規頁，主題「法規草案預告」。
- **bank_shelf**（`adapters/bank_shelf.py`、`bank_shelf` 表）：每個銀行列表頁一個 item，正規化成「保險公司＋商品名稱」後以指紋比對（頁面上的快取參數不算改版，D11）。新上架寫 `first_seen`、消失寫 `removed_at`（不刪除）；列表頁第一次抓到只建立基準（`baseline=1`）；解析到 0 筆或少於目前上架數一半時丟錯、不判定下架（同 D20）。下架又重新出現時，內容會跟舊版本相同而被判為未變動，所以 `change_detect` 新增選用的 `after_revert` 掛點讓上架狀態跟著更新。兆豐是商品頁背後的 JSON API（POST），`PoliteClient` 因此新增 `post()`；永豐的項目帶上下架時間、頁面用 JS 隱藏過期項目，這裡照同樣的時間過濾。不收：中國信託（JS 機器人驗證，不繞過）、星展（網頁不列商品名稱）。匯出時最近 30 天的上架／下架（不含基準）變成「通路動態」情報。
- **declared_rates**（`adapters/declared_rates.py`、`declared_rates` 表）：每家公司一個 item，只收名稱同時含「美元」「利率變動」的商品。國泰（一個 GET 回傳全部歷史，約 7 MB）、保誠（靜態頁）一次就有完整歷史；凱基、台灣人壽、南山按月份／分頁查詢，資料庫還沒有該公司歷史時回補 `backfill_months`，之後只抓 `recent_months`。南山的 API 連 GET 都要帶 `Content-Type: application/json`（否則 406），`PoliteClient.get()` 因此可帶 headers；台灣人壽是 JSON POST。每月 1–5 日跑。不收：安聯（Cloudflare）、友邦（連線逾時）、台新（2026-01 併入新光，新來源待確認）、富邦（HTML 分頁量大，待做）、安達（每張商品一個 PDF，待做）、法巴（官網只公告貨幣帳戶與萬能利率）。
- **匯出到雷達網站**：`rates` 不挑代表商品（挑「最高的那張」會有選擇偏誤），改給每家公司的分布：最新月份的最低／四分位／中位數／最高、與上個月相比調升／調降的張數、調幅最大的商品，以及 7 個月的中位數趨勢。趨勢只用 7 個月都有公告的商品（固定樣本），避免新舊商品替換造成假的漲跌。

### 自動更新雷達網站（2026-10-07 新增）

雷達網站（`../../paris-hackathon-business-competition`）只顯示這個爬蟲產生的真實資料。一個指令跑完整條流程：

```bash
python scripts/update_radar.py              # 全部來源爬一輪 → 解析 → 匯出 real-data.js 到網站資料夾（不 push）
python scripts/update_radar.py --push       # 同上，real-data.js 有變動就 commit 並 push（Vercel 自動重新部署）
python scripts/update_radar.py --skip-crawl # 只重新解析、匯出（改了匯出規則時用）

sh scripts/schedule_radar.sh install        # macOS launchd：每天 08:00、18:00 自動跑 --push（RADAR_HOURS="7 19" 可改時間）
sh scripts/schedule_radar.sh run            # 立刻用排程設定跑一次
sh scripts/schedule_radar.sh status         # 排程狀態＋最近一次結果
sh scripts/schedule_radar.sh uninstall
```

- 部署：Vercel Hobby 專案只接受專案擁有者本人的 commit，GitHub 觸發的部署會被擋，所以 `--push` 之後改用 Vercel CLI 從網站資料夾直接部署（`radar.vercel_deploy: true`）。網站資料夾已 `vercel link` 到 jousinliaaron1224 的 `product-intel-radar`，正式網址 https://product-intel-radar-taupe.vercel.app 。換電腦時要先 `npx vercel login`、在網站資料夾 `npx vercel link`。
- 設定在 `config/sources.yaml` 的 `radar`：`site_dir`（網站資料夾）、`git_branch`（只有網站資料夾目前在這個分支時才 push，預設 `main`；在其他分支時只匯出、不 push）。只 commit `real-data.js`，網站其他未提交的修改不會被帶進去。
- 單一來源失敗不中斷，照樣用資料庫現有資料匯出；匯出失敗就不 commit，網站維持上一版。結果寫在 `data/logs/last_update.json`，每月一個 log 檔 `data/logs/update_radar-YYYYMM.log`；同時只會有一個程序在跑（檔案鎖）。
- 專案放在桌面時，macOS 會擋背景程式讀桌面（`Operation not permitted`，看 `data/logs/launchd.err`）：到「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」加入 `status` 印出的 Python 執行檔，或把兩個專案移出桌面。
- 匯出時自動產生的情報（不靠人工整理）：宣告利率每家公司每月的調升／調降（`N7xx`，最新月份全部持平也發一則）、近 90 天新核准的投資型商品（`N8xx`，同公司同一天合併）、銀行上架／下架（`N9xx`），加上金管會新聞稿／裁罰／草案、新聞 RSS 的規則分類（`N5xx`）。
- 分紅、房貸壽險、美元利變商品來自銀行上架清單（兩家銀行寫法不同的同一張商品會合併、記下所有上架銀行），美元利變商品用名稱比對帶入官網宣告利率。只有名稱、險種、幣別，費用與保費門檻一律留空，不估計。
- 外幣保單占比：`fsc_press` 的 `attachment_tables` 命中附件名稱時下載 PDF、用 pdfplumber 解析表格存進 `meta.tables`；每月「外幣保險商品銷售情形」新聞稿出來就會自動更新。
- 新聞 RSS 加了經濟日報「金融」「產業」（關鍵字過濾）。Google 新聞 RSS 不用：`news.google.com` 的 robots.txt 禁止 `/rss`。RSS 只有最新幾十則，新聞會隨排程逐步累積，不會回補歷史。

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
| `parsers/pipeline.py` | M4：事件消費者（重試、失敗警示） |
| `parsers/clause_pdf.py`、`parsers/clause.py` | 條款 PDF 結構化、統一商品 schema、逐條 diff |
| `parsers/classify.py` | 新聞／法規分類與影響程度 |
| `parsers/base.py` | `ParseContext`、LLM 補強介面 `Enricher` |
| `demo/snapshot.py`、`demo/replay.py` | demo 快照與重播 |

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
