from datetime import datetime, timezone

from core import events
from core.storage import Database, RawStore


def test_schema_has_all_tables(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.init_schema()  # 重跑不出錯
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"seen_items", "raw_docs", "events", "products", "crawl_runs"} <= names


def test_raw_store_is_content_addressed(tmp_path):
    store = RawStore(tmp_path / "data" / "raw")
    t1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    p1, h1 = store.put("src", b"hello", "html", t1)
    p2, h2 = store.put("src", b"hello", "html", t2)  # 隔天同內容：不重複落地
    assert (p1, h1) == (p2, h2)
    assert p1 == f"raw/src/2026-10-01/{h1}.html"
    assert store.read(p1) == b"hello"
    assert len(list((tmp_path / "data" / "raw").rglob("*.html"))) == 1


def test_event_type_is_checked(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    events.emit(db, events.NEW_ITEM, None, {"a": 1})
    try:
        events.emit(db, "bogus", None, {})
        raise AssertionError("should reject")
    except ValueError:
        pass
    pend = events.pending(db)
    assert len(pend) == 1 and pend[0]["payload"] == {"a": 1}
    events.mark_processed(db, [pend[0]["id"]])
    assert events.pending(db) == []
