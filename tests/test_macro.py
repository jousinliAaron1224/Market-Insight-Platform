import json
from types import SimpleNamespace

from adapters.macro import parse_cbc, parse_fred


def test_parse_fred_skips_missing_values():
    text = "observation_date,DGS10\n2026-10-13,5.05\n2026-10-14,.\n2026-10-15,\n2026-10-16,5.1\n"
    assert parse_fred(text) == [["2026-10-13", 5.05], ["2026-10-16", 5.1]]


def test_parse_cbc_converts_dates_and_skips_bom():
    text = '﻿"日","重貼現(%)","擔保放款融通(%)","短期融通(%)"\n"20230324","1.875","2.25","4.125"\n"20240322","2","2.375","4.25"\n'
    assert parse_cbc(text) == [["2023-03-24", 1.875, 2.25, 4.125], ["2024-03-22", 2.0, 2.375, 4.25]]


def test_macro_export_uses_latest_version(tmp_path):
    from core.storage import Database
    from scripts.export_radar import macro
    db = Database(tmp_path / "intel.db")
    db.init_schema()

    def put(key, ver, meta):
        db.conn.execute("""INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
                           VALUES ('macro', ?, ?, ?, 'x', '2026-10-19', 'u', 'csv', ?)""", (key, ver, f"h{key}{ver}", json.dumps(meta)))
    fred = lambda sid, pts: {"title": sid, "series": sid, "kind": "fred", "unit": "%", "points": pts}  # noqa: E731
    put("macro:ust10", 1, fred("ust10", [["2025-10-15", 4.0]]))   # 舊版本：應該被忽略
    put("macro:ust10", 2, fred("ust10", [["2025-10-15", 4.05], ["2026-09-15", 5.0], ["2026-09-30", 5.29], ["2026-10-15", 5.1]]))
    put("macro:fed", 1, fred("fed", [["2026-09-16", 3.75], ["2026-09-17", 4.0], ["2026-10-18", 4.0]]))
    put("macro:cbc", 1, {"title": "央行", "series": "cbc", "kind": "cbc", "points": [["2010-01-01", 1.25, 1.625, 3.5], ["2023-03-24", 1.875, 2.25, 4.125], ["2024-03-22", 2.0, 2.375, 4.25]]})
    db.conn.commit()
    rates = {"market_trend": [{"m": "2026-09", "med": 3.4, "n": 10}, {"m": "2026-10", "med": 3.4, "n": 10}]}
    out = macro(SimpleNamespace(db_path=str(tmp_path / "intel.db")), rates)
    u = out["series"]["ust10"]
    assert u["latest"] == ["2026-10-15", 5.1] and u["m1"] == ["2026-09-15", 5.0] and u["y1"] == ["2025-10-15", 4.05]
    assert u["monthly"] == [["2025-10", 4.05], ["2026-09", 5.29], ["2026-10", 5.1]]   # 月底值
    assert out["series"]["fed"]["last_change"] == {"date": "2026-09-17", "from": 3.75, "to": 4.0}
    assert [s[0] for s in out["cbc"]["steps"]] == ["2023-03-24", "2024-03-22"]          # 只留 2015 年起
    assert out["spread"][-1] == {"m": "2026-10", "declared": 3.4, "ust10": 5.1, "spread": -1.7}
    assert any("自 2024-03-22 起未調整" in c for c in out["conclusions"])


def test_macro_export_without_data(tmp_path):
    from core.storage import Database
    from scripts.export_radar import macro
    Database(tmp_path / "intel.db").init_schema()
    assert macro(SimpleNamespace(db_path=str(tmp_path / "intel.db"))) is None
