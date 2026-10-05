"""排程器：sources.yaml 的 cron 正確轉成工作；工作例外不讓排程器停止。"""
import logging
from datetime import datetime

from core.config import load_config
from scheduler import run as runner


def test_jobs_built_from_sources_yaml():
    cfg = load_config()
    sched = runner.build_scheduler(cfg)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"tii_law_rss", "fsc_press", "fsc_penalty", "news_rss",
                         "company_cardif_products", "company_cathay_products", "company_fubon_products",
                         "company_taiwanlife_products", "company_kgi_products", "open_data", "parse_events"}
    for j in jobs.values():
        assert j.max_instances == 1 and j.coalesce is True
    fields = {f.name: str(f) for f in jobs["fsc_press"].trigger.fields}
    assert fields["minute"] == "15" and fields["hour"] == "*/4"
    assert str(jobs["fsc_press"].trigger.timezone) == "Asia/Taipei"
    # 各來源錯開分鐘數，避免整點同時打出去
    nxt = jobs["fsc_press"].trigger.get_next_fire_time(None, datetime(2026, 10, 3, 9, 0, tzinfo=jobs["fsc_press"].trigger.timezone))
    assert (nxt.hour, nxt.minute) == (12, 15)


def test_disabled_source_not_scheduled():
    cfg = load_config()
    for s in cfg["sources"]:
        if s["id"] == "fsc_penalty":
            s["enabled"] = False
    assert "fsc_penalty" not in runner.implemented_sources(cfg)


def test_job_crash_is_logged_not_raised(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("adapter exploded")
    monkeypatch.setattr(runner, "run_once", boom)
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        runner.run_job("fsc_press", {}, None)
    assert "job crashed" in caplog.text and "adapter exploded" in caplog.text
