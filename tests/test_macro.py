from datetime import date, timedelta
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


def test_parse_cbc_bond_monthly_10y():
    from adapters.macro import parse_cbc_bond
    text = '﻿"月","政府公債-3年期(%)","十年期政府公債次級市場利率(%)"\n"2026M07","-","1.90030"\n"2026M08","-","-"\n"2026M09","1.5","1.955"\n'
    assert parse_cbc_bond(text) == [["2026-07-01", 1.9003], ["2026-09-01", 1.955]]


def test_parse_dgbas_cpi_takes_yoy_by_item_prefix():
    from adapters.macro import parse_dgbas_cpi
    obs = lambda item, ym, typ, v: f"<Obs><Item>{item}(指數基期：民國110年=100)</Item><TIME_PERIOD>{ym}</TIME_PERIOD><FREQ>M</FREQ><TYPE>{typ}</TYPE>\n<Item_VALUE>{v}</Item_VALUE></Obs>\n"  # noqa: E731
    text = "".join([obs("總指數", "2026M09", "原始值", "110.5"), obs("總指數", "2026M09", "年增率(%)", "2.73"), obs("五.醫藥保健類", "2026M09", "年增率(%)", "0.7"),
                    obs("1.醫療費用", "2026M08", "年增率(%)", ""), obs("1.醫療費用", "2026M09", "年增率(%)", "1.05")])
    out = parse_dgbas_cpi(text, {"tw_cpi": "總指數", "tw_med": "五.醫藥保健類", "tw_medfee": "1.醫療費用"})
    assert out == {"tw_cpi": [["2026-09-01", 2.73]], "tw_med": [["2026-09-01", 0.7]], "tw_medfee": [["2026-09-01", 1.05]]}


def test_parse_dgbas_wage_monthly_only():
    from adapters.macro import parse_dgbas_wage
    blk = lambda ym, a, b: f"<每人每月經常性薪資><年月別_Year_and_month>{ym}</年月別_Year_and_month><工業及服務業__Industry_and_services_金額_新臺幣元>{a}</工業及服務業__Industry_and_services_金額_新臺幣元><金融及保險業_Financial_and_insurance_activities_金額_新臺幣元>{b}</金融及保險業_Financial_and_insurance_activities_金額_新臺幣元></每人每月經常性薪資>"  # noqa: E731
    text = blk("2025", 47884, 70000) + blk("202607Ⓡ", 49466, 76838) + blk("202608Ⓟ", 49518, 76152)
    out = parse_dgbas_wage(text, {"w": "工業及服務業__", "f": "金融及保險業"})
    assert out == {"w": [["2026-07-01", 49466.0], ["2026-08-01", 49518.0]], "f": [["2026-07-01", 76838.0], ["2026-08-01", 76152.0]]}


def test_macro_export_yoy_cross_rate_and_assets(tmp_path):
    from core.storage import Database
    from scripts.export_radar import macro
    db = Database(tmp_path / "intel.db")
    db.init_schema()
    n = [0]

    def put(sid, pts, **extra):
        n[0] += 1
        meta = {"title": sid, "series": sid, "kind": "fred", "unit": "%", "points": pts, **extra}
        db.conn.execute("""INSERT INTO raw_docs (source_id, item_key, version, content_hash, raw_path, fetched_at, url, doc_type, meta)
                           VALUES ('macro', ?, 1, ?, 'x', '2026-10-19', 'u', 'csv', ?)""", ("macro:" + sid, f"h{n[0]}", json.dumps(meta)))
    put("us_cpi", [["2025-09-01", 100.0], ["2026-08-01", 102.0], ["2026-09-01", 103.0]], transform="yoy", title="美國 CPI")
    put("usdtwd", [["2026-10-15", 32.0], ["2026-10-16", 32.4]])
    put("jpyusd", [["2026-10-16", 160.0]])
    days = [(date(2025, 10, 1) + timedelta(days=i)).isoformat() for i in range(400)]
    put("sp500", [[d, 100 + i * 0.1] for i, d in enumerate(days)], title="美股 S&P 500")
    db.conn.commit()
    out = macro(SimpleNamespace(db_path=str(tmp_path / "intel.db")))
    cpi = out["series"]["us_cpi"]
    assert cpi["name"] == "美國 CPI 年增率" and cpi["unit"] == "%" and cpi["latest"] == ["2026-09-01", 3.0] and cpi["level"] == ["2026-09-01", 103.0]
    assert out["series"]["twd_jpy"]["latest"] == ["2026-10-16", 20.25]   # 32.4 / 160 × 100
    a = out["assets"][0]
    assert a["id"] == "sp500" and a["ret_1y"] > 0 and a["mdd_1y"] == 0 and a["vol_1y"] > 0
