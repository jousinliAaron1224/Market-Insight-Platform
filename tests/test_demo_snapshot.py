"""Demo 快照（M4，D24）：自足、可驗證、不含新聞導言。"""
import json
import sqlite3
from datetime import timedelta

import pytest

from adapters.tii_law_rss import TiiLawRssAdapter
from core.change_detect import run_source, validator_lookup
from core.storage import Database, RawStore, utcnow
from demo import snapshot
from tests.conftest import make_ctx
from tests.test_change_detect import CFG, make_site


@pytest.fixture
def env(tmp_path):
    """用保發中心 fixture 跑一輪，得到真實格式的 data/intel.db 與 data/raw。"""
    import httpx
    from core.http import PoliteClient
    from core.ratelimit import DomainRateLimiter
    data = tmp_path / "data"
    db = Database(data / "intel.db")
    db.init_schema()
    raw = RawStore(data / "raw")
    site = make_site()
    http = PoliteClient("T/0.1", DomainRateLimiter(0), transport=httpx.MockTransport(site.handler))
    run_source(TiiLawRssAdapter(make_ctx(raw, http, validator_lookup(db)), CFG), db)
    db.put_lead("news_rss", "k", None, "導言全文", utcnow(), 30)
    db.conn.commit()
    db.close()
    cfg = {"defaults": {"db_path": str(data / "intel.db"), "raw_root": str(data / "raw")}}
    return cfg, data


def test_create_and_verify(env):
    cfg, data = env
    dest = snapshot.create("demo", cfg, make_zip=True)
    assert dest == data / "snapshots" / "demo" and (data / "snapshots" / "demo.zip").is_file()
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert m["counts"]["events_by_type"] == {"new_item": 50}
    assert len(m["files"]) == 100                      # 50 個內文 html + 50 個 RSS item json（meta 的 rss_item_path）
    r = snapshot.verify("demo", cfg)
    assert r["ok"] and r["files"] == 100
    conn = sqlite3.connect(dest / "intel.db")
    assert conn.execute("SELECT COUNT(*) FROM news_leads").fetchone()[0] == 0   # 導言不進快照（D16）
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"        # 單一檔案
    assert [s["name"] for s in snapshot.list_snapshots(cfg)] == ["demo"]


def test_snapshot_is_immutable_and_tamper_detected(env):
    cfg, data = env
    dest = snapshot.create("demo", cfg)
    with pytest.raises(FileExistsError):
        snapshot.create("demo", cfg)
    f = next((dest / "raw" / "tii_law_rss").rglob("*.html"))
    f.write_bytes(b"tampered")
    r = snapshot.verify("demo", cfg)
    assert not r["ok"] and any("雜湊不符" in p for p in r["problems"])


def test_missing_raw_is_reported(env):
    cfg, data = env
    victim = next((data / "raw" / "tii_law_rss").rglob("*.html"))
    victim.unlink()
    dest = snapshot.create("demo", cfg)
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert len(m["missing_raw"]) == 1
    assert snapshot.verify("demo", cfg)["ok"]           # 建立時就已記錄的缺檔不算快照損壞
