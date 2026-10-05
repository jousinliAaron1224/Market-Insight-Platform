"""政府資料開放平臺下載器（D28）：API 檔案清單 → 下載存 raw；格式不符時錯誤訊息要列出實際欄位。"""
import json

import pytest

from adapters.open_data import API, OpenDataAdapter, ext_for, parse_dataset
from core import events
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, make_ctx

# 測試用合成回應（欄位名稱依平臺 API v2 的格式；實際格式要在 Mac 上確認）
DS = {"success": True, "result": {"identifier": "14539", "title": "壽險業績統計", "modifiedDate": "2026-09-30 10:00:00",
      "distribution": [{"resourceID": "r1", "resourceDescription": "壽險業績統計", "resourceFormat": "CSV",
                        "resourceDownloadUrl": "https://example.org/lia.csv"}]}}
CSV = "年月,險種,新契約保費\n11508,投資型,100\n".encode("utf-8")
CFG = {"datasets": [{"id": 14539, "name": "lia_performance", "purpose": "demand"}]}


def test_parse_dataset_and_ext():
    info = parse_dataset(json.dumps(DS).encode(), "14539")
    assert info["title"] == "壽險業績統計" and info["files"][0]["format"] == "csv"
    assert ext_for("csv", "https://x/a", None) == "csv"
    assert ext_for("", "https://x/a.xlsx?x=1", None) == "xlsx"
    assert ext_for("", "https://x/a", "application/json") == "json"
    with pytest.raises(ValueError, match="實際欄位"):
        parse_dataset(json.dumps({"result": {"foo": 1}}).encode(), "1")


def test_download_store_and_revise(make_env):
    site = FakeSite({API.format(id=14539): (200, json.dumps(DS).encode(), {}),
                     "https://example.org/lia.csv": (200, CSV, {})})
    db, raw, http = make_env(site)
    ad = OpenDataAdapter(make_ctx(raw, http, validator_lookup(db)), CFG)
    st = run_source(ad, db)
    assert (st.listed, st.new_items, st.errors) == (1, 1, 0)
    row = db.conn.execute("SELECT * FROM raw_docs").fetchone()
    assert row["doc_type"] == "csv" and raw.read(row["raw_path"]) == CSV
    meta = json.loads(row["meta"])
    assert meta["dataset_id"] == "14539" and meta["purpose"] == "demand"
    # 資料更新（資料集更新時間變了、檔案內容也變了）→ doc_revised
    DS2 = json.loads(json.dumps(DS))
    DS2["result"]["modifiedDate"] = "2026-10-31 10:00:00"
    site.routes[API.format(id=14539)] = (200, json.dumps(DS2).encode(), {})
    site.routes["https://example.org/lia.csv"] = (200, CSV + "11509,投資型,120\n".encode(), {})
    st = run_source(ad, db)
    assert st.revised == 1 and events.pending(db, {events.DOC_REVISED})


def test_one_dataset_failing_is_partial(make_env):
    cfg = {"datasets": CFG["datasets"] + [{"id": 104113, "name": "ib_premium_monthly"}]}
    site = FakeSite({API.format(id=14539): (200, json.dumps(DS).encode(), {}),
                     "https://example.org/lia.csv": (200, CSV, {})})          # 104113 → 404
    db, raw, http = make_env(site)
    st = run_source(OpenDataAdapter(make_ctx(raw, http, validator_lookup(db)), cfg), db)
    assert st.new_items == 1 and st.errors == 1
    assert db.conn.execute("SELECT status FROM crawl_runs").fetchone()[0] == "partial"
