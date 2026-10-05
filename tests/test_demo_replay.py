"""重播事件（M4，D24）：依真實日期重建時間線、在工作副本上跑解析層、不動快照。"""
import json

import pytest

from core import events
from core.storage import Database, RawStore, utcnow
from demo import replay as rp
from demo import snapshot
from tests.conftest import fixture_bytes

KGI = fixture_bytes("companies", "kgi", "clause_sample.pdf")
TII = fixture_bytes("tii_law_rss", "shownews_3867.html")


def add(db, raw, src, key, ext, data, meta, doc_type, version=1, prev=None, created="2026-10-03T02:00:00+00:00"):
    path, digest = raw.put(src, data, ext, utcnow())
    rid = db.conn.execute(
        """INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
           VALUES (?,?,?,?,?, '2026-10-03T00:00:00+00:00', 'https://x', ?, ?)""",
        (src, key, version, digest + str(version), path, doc_type, json.dumps(meta, ensure_ascii=False))).lastrowid
    pl = {"source_id": src, "item_key": key, "title": meta["title"], "published_at": meta.get("published_at"),
          "doc_type": doc_type, "raw_path": path, "version": version, **(meta.get("event_extra") or {})}
    if prev:
        pl.update(previous_raw_doc_id=prev, previous_version=version - 1)
    eid = events.emit(db, events.DOC_REVISED if prev else events.NEW_ITEM, rid, pl)
    db.conn.execute("UPDATE events SET created_at=? WHERE id=?", (created, eid))
    return rid


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    db = Database(data / "intel.db")
    db.init_schema()
    raw = RawStore(data / "raw")
    tii = lambda n, d: {"title": f"投資型保險商品銷售應注意事項{n}", "published_at": f"{d}T00:00:00+08:00",
                        "data_type": "行政規則"}
    add(db, raw, "tii_law_rss", "t-old", "html", TII + b"1", tii(1, "2026-03-01"), "html")
    add(db, raw, "tii_law_rss", "t-new", "html", TII + b"2", tii(2, "2026-09-20"), "html")
    co = {"company": "凱基人壽", "line": "變額年金保險", "currency": "USD"}
    names = ["凱基人壽新上架外幣變額年金保險", "凱基人壽剛修正外幣變額年金保險", "凱基人壽老商品外幣變額年金保險"]
    dates = [("2026-09-01", "2026-09-01"), ("2020-01-01", "2026-08-15"), ("2019-01-01", "2024-01-01")]
    for i, (n, (f, l)) in enumerate(zip(names, dates)):
        add(db, raw, "company_kgi_products", f"k:{n}", "pdf", KGI + str(i).encode(),
            {**co, "title": n, "first_date": f, "latest_date": l, "event_extra": {"company": "凱基人壽"}}, "pdf")
        db.conn.execute("INSERT INTO products (company, name, line, currency, status) VALUES (?,?,?,?, 'on_sale')",
                        ("凱基人壽", n, "變額年金保險", "USD"))
    db.conn.commit()
    db.close()
    cfg = {"defaults": {"db_path": str(data / "intel.db"), "raw_root": str(data / "raw")},
           "parsing": {"classify": __import__("core.config", fromlist=["x"]).load_config()["parsing"]["classify"]}}
    snapshot.create("demo", cfg)
    return cfg, data


def test_timeline_reconstruction(env):
    cfg, data = env
    import sqlite3
    conn = sqlite3.connect(data / "snapshots" / "demo" / "intel.db")
    steps = rp.build_timeline(conn, "2026-07-05", "2026-10-03")
    live = [(s.at, s.type, s.payload.get("title")[:8], s.payload.get("reconstructed")) for s in steps if not s.baseline]
    assert live == [
        ("2026-08-15", "doc_revised", "凱基人壽剛修正外", True),
        ("2026-09-01", "product_launched", "凱基人壽新上架外", True),
        ("2026-09-01", "new_item", "凱基人壽新上架外", False),
        ("2026-09-20", "new_item", "投資型保險商品銷", False),
    ]
    base = [s.payload.get("title")[:8] for s in steps if s.baseline]
    assert sorted(base) == sorted(["投資型保險商品銷", "凱基人壽剛修正外", "凱基人壽老商品外"])
    assert all(s.payload["replay"] for s in steps)


def test_replay_runs_parse_layer_on_work_copy(env):
    cfg, data = env
    lines = []
    st = rp.replay("demo", cfg, days=90, as_of="2026-10-03", out=lines.append)
    assert st["errors"] == 0 and st["baseline"] == 3 and st["replayed"] == 4
    assert st["by_type"] == {"doc_revised": 1, "product_launched": 1, "new_item": 2}
    text = "\n".join(lines)
    assert "＋上架  凱基人壽｜凱基人壽新上架外幣變額年金保險（依核准日重建）" in text
    assert "條款改版 36 條" in text and "修正日 2026-08-15（依修正日重建）" in text
    assert "★高" in text
    assert st["product_terms"] == {"products": 3, "with_clause": 3}
    # 快照不動、工作副本有結果
    assert snapshot.verify("demo", cfg)["ok"]
    work = Database(data / "replay" / "demo" / "intel.db")
    assert work.conn.execute("SELECT COUNT(*) FROM events WHERE processed=0").fetchone()[0] == 0
    assert work.conn.execute("SELECT COUNT(*) FROM doc_labels").fetchone()[0] == 2
    log = (data / "replay" / "demo" / "replay_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 7


def test_replay_is_repeatable(env):
    cfg, data = env
    a = rp.replay("demo", cfg, days=90, as_of="2026-10-03", out=lambda s: None)
    b = rp.replay("demo", cfg, days=90, as_of="2026-10-03", out=lambda s: None)
    assert {k: v for k, v in a.items() if k != "workdir"} == {k: v for k, v in b.items() if k != "workdir"}
