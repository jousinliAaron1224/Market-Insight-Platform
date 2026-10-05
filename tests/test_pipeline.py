"""解析層事件消費者（M4）：成功／略過／重試／失敗警示，與商品上架停售狀態。"""
from core import events
from core.storage import Database, RawStore
from parsers.base import ParseContext
from parsers.pipeline import process_pending
from parsers.products import ProductEventHandler, product_row


def make_ctx(tmp_path, **cfg):
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    return ParseContext(db=db, raw=RawStore(tmp_path / "raw"), config=cfg)


class Flaky:
    name = "flaky"

    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def handles(self, ev):
        return ev["payload"].get("source_id") == "s"

    def handle(self, ctx, ev):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return {"x": 1}


def emit(ctx, src="s", type_=events.NEW_ITEM, **pl):
    return events.emit(ctx.db, type_, None, {"source_id": src, "item_key": "k", "title": "t", **pl})


def status(ctx, eid):
    return ctx.db.conn.execute("SELECT status, attempts FROM event_processing WHERE event_id=?", (eid,)).fetchone()


def test_ok_and_skipped(tmp_path):
    ctx = make_ctx(tmp_path)
    a, b = emit(ctx), emit(ctx, src="other")
    seen = []
    st = process_pending(ctx, [Flaky(0)], on_event=lambda e, s, sm: seen.append((e["id"], s)))
    assert st == {"ok": 1, "skipped": 1, "error": 0, "failed": 0}
    assert seen == [(a, "ok"), (b, "skipped")]
    assert events.pending(ctx.db) == []
    assert tuple(status(ctx, a)) == ("ok", 1)


def test_error_retried_next_run_then_ok(tmp_path):
    ctx = make_ctx(tmp_path)
    eid = emit(ctx)
    h = Flaky(1)
    assert process_pending(ctx, [h])["error"] == 1
    assert len(events.pending(ctx.db)) == 1          # 失敗的事件留著下一輪再試
    assert process_pending(ctx, [h])["ok"] == 1
    assert tuple(status(ctx, eid)) == ("ok", 2)


def test_failed_after_max_attempts_raises_alert_and_resolves(tmp_path):
    ctx = make_ctx(tmp_path, max_attempts=2)
    eid = emit(ctx)
    h = Flaky(5)
    process_pending(ctx, [h])
    st = process_pending(ctx, [h])
    assert st["failed"] == 1 and events.pending(ctx.db) == []
    assert status(ctx, eid)["status"] == "failed"
    alert = ctx.db.conn.execute("SELECT * FROM alerts WHERE source_id='parser'").fetchone()
    assert alert and alert["resolved_at"] is None
    emit(ctx)                                        # 之後一輪全部成功 → 警示解除
    process_pending(ctx, [Flaky(0)])
    assert ctx.db.conn.execute("SELECT resolved_at FROM alerts WHERE source_id='parser'").fetchone()[0]


def test_one_bad_event_does_not_block_others(tmp_path):
    ctx = make_ctx(tmp_path)

    class Picky(Flaky):
        def handle(self, ctx, ev):
            if ev["payload"].get("bad"):
                raise ValueError("bad pdf")
            return {}
    emit(ctx, bad=True)
    emit(ctx)
    st = process_pending(ctx, [Picky(0)])
    assert (st["ok"], st["error"]) == (1, 1)


def test_product_events_update_status(tmp_path):
    ctx = make_ctx(tmp_path)
    base = {"company": "國泰人壽", "title": "國泰人壽X變額年金保險", "line": "變額年金保險", "currency": "TWD"}
    emit(ctx, src="company_cathay_products", type_=events.PRODUCT_LAUNCHED, **base)
    process_pending(ctx, [ProductEventHandler()])
    assert product_row(ctx.db, "國泰人壽", base["title"])["status"] == "on_sale"
    emit(ctx, src="company_cathay_products", type_=events.PRODUCT_DISCONTINUED, **base)
    process_pending(ctx, [ProductEventHandler()])
    assert product_row(ctx.db, "國泰人壽", base["title"])["status"] == "discontinued"


def test_schema_migrates_existing_db(tmp_path):
    """Mac 上既有的 intel.db 只要 init_schema 就會補上 M4 的表，舊資料不動。"""
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    events.emit(db, events.NEW_ITEM, None, {"source_id": "s"})
    db.conn.commit()
    db.init_schema()
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"event_processing", "clause_docs", "clause_articles", "clause_diffs",
            "product_terms", "doc_labels"} <= names
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
