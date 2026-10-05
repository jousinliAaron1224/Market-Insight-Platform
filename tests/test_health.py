"""健康檢查：連續 0 筆、欄位空值比例、整輪錯誤；警示去重與自動解除。"""
import json
from datetime import timedelta

from core import health
from core.storage import Database, iso, utcnow


def db_(tmp_path):
    db = Database(tmp_path / "h.db")
    db.init_schema()
    return db


def add_run(db, sid, listed, status="ok", err=None, started=None):
    started = started or iso(utcnow())
    db.conn.execute(
        "INSERT INTO crawl_runs (source_id, started_at, finished_at, status, items_listed, errors, error_detail)"
        " VALUES (?,?,?,?,?,?,?)", (sid, started, started, status, listed, 1 if err else 0, err))
    db.conn.commit()
    return started


def open_alerts(db, sid):
    return {r["kind"]: r["occurrences"] for r in db.conn.execute(
        "SELECT kind, occurrences FROM alerts WHERE source_id=? AND resolved_at IS NULL", (sid,))}


def test_zero_listed_three_runs_raises_then_resolves(tmp_path):
    db = db_(tmp_path)
    for _ in range(2):
        add_run(db, "s", 0)
        assert health.check_after_run(db, "s", {}).raised == []
    add_run(db, "s", 0)
    r = health.check_after_run(db, "s", {})
    assert [k for k, _ in r.raised] == ["zero_listed"]
    add_run(db, "s", 0)
    health.check_after_run(db, "s", {})
    assert open_alerts(db, "s") == {"zero_listed": 2}  # 去重：同一筆累加次數
    add_run(db, "s", 5)
    r = health.check_after_run(db, "s", {})
    assert r.resolved == ["zero_listed"] and open_alerts(db, "s") == {}


def test_allow_empty_runs_skips_zero_rule(tmp_path):
    db = db_(tmp_path)
    for _ in range(4):
        add_run(db, "news", 0)
    assert health.check_after_run(db, "news", {"allow_empty_runs": True}).raised == []


def test_missing_fields_ratio(tmp_path):
    db = db_(tmp_path)
    started = iso(utcnow() - timedelta(seconds=5))
    add_run(db, "s", 4, started=started)
    for i, warn in enumerate([True, True, False, False]):
        meta = {"parse_warnings": ["missing title"]} if warn else {}
        db.conn.execute(
            "INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)"
            " VALUES ('s', ?, 1, ?, 'p', ?, 'u', 'html', ?)", (f"k{i}", f"h{i}", iso(utcnow()), json.dumps(meta)))
    db.conn.commit()
    r = health.check_after_run(db, "s", {})
    assert [k for k, _ in r.raised] == ["missing_fields"]
    assert "4 筆中有 2 筆" in r.raised[0][1]


def test_run_errors_alert_and_resolve(tmp_path):
    db = db_(tmp_path)
    add_run(db, "s", 0, status="error", err="list_items: ConnectError('x')\ntrace")
    r = health.check_after_run(db, "s", {})
    assert ("run_errors" in [k for k, _ in r.raised]) and "ConnectError" in r.raised[-1][1]
    add_run(db, "s", 3)
    r = health.check_after_run(db, "s", {})
    assert "run_errors" in r.resolved


def test_summary_lists_last_success_and_alerts(tmp_path):
    db = db_(tmp_path)
    add_run(db, "a", 5)
    add_run(db, "a", 0, status="error", err="boom")
    health.check_after_run(db, "a", {})
    s = {x["source_id"]: x for x in health.summary(db, ["a", "never_ran"])}
    assert s["a"]["last_status"] == "error" and s["a"]["last_success"] is not None
    assert s["a"]["open_alerts"][0]["kind"] == "run_errors"
    assert s["never_ran"]["last_status"] == "never"
