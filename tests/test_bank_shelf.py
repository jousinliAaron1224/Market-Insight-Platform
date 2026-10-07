"""bank_shelf（銀行通路上架）：fixture 是 2026-10-05 實際抓到的兆豐 API、華南投資型頁、永豐人身保險頁。"""
import json
from datetime import datetime

import pytest

from adapters.bank_shelf import (BankShelfAdapter, line_of, norm_insurer, parse_hncb, parse_mega,
                                 parse_sinopac, split_insurer)
from adapters.common import TPE
from core.change_detect import run_source, validator_lookup
from tests.conftest import FakeSite, fixture_bytes, make_ctx

MEGA = fixture_bytes("bank_shelf", "mega.json")
HNCB = fixture_bytes("bank_shelf", "hncb_investment.html")
SINOPAC = fixture_bytes("bank_shelf", "sinopac_life.html")
MEGA_URL = "https://mega.test/api/GetInsurances"
CFG = {"banks": [{"id": "b_mega", "name": "兆豐銀行", "kind": "mega_api",
                  "pages": [{"key": "all", "label": "總覽", "url": MEGA_URL, "form": {"itemID": "{X}"}}]}]}
NOW = datetime(2026, 10, 5, 12, tzinfo=TPE)


def test_normalize_names():
    assert norm_insurer("法商法國巴黎人壽") == "法國巴黎人壽"
    assert norm_insurer("新光人壽 (原：台新人壽)") == "新光人壽"
    assert split_insurer("友邦人壽鍾愛一生美元利率變動型還本終身保險") == ("友邦人壽", "鍾愛一生美元利率變動型還本終身保險")
    assert split_insurer("法商法國巴黎人壽價值大師外幣變額萬能壽險")[0] == "法國巴黎人壽"
    assert split_insurer("（無保險公司）某專案") == (None, "（無保險公司）某專案")


def test_line_of():
    assert line_of("價值大師外幣變額萬能壽險") == "investment"
    assert line_of("保築感定期壽險", "房貸壽險") == "mortgage_term"
    assert line_of("珍豐收美元分紅終身壽險") == "participating"
    assert line_of("超美得美元利率變動型終身壽險") == "usd_interest"
    assert line_of("日優寶日圓利率變動型終身壽險") == "other"     # 非美元的利變不算
    assert line_of("鑫滿安康終身壽險(定期給付型)") == "other"


def test_parse_mega():
    rows = parse_mega(MEGA)
    assert len(rows) == 99
    cardif = [r for r in rows if r["insurer"] == "法國巴黎人壽"]
    assert len(cardif) == 7 and all(not r["product"].startswith("法國巴黎人壽") for r in cardif)
    assert any(r["insurer"] == "新光人壽" for r in rows)          # 「新光人壽 (原：台新人壽)」
    assert {r["line"] for r in rows if r["category"] == "房貸壽險"} == {"mortgage_term"}


def test_parse_hncb_cards():
    rows = parse_hncb(HNCB, "投資型保險")
    assert len(rows) == 16
    assert {r["insurer"] for r in rows} == {"法國巴黎人壽", "安達人壽", "國泰人壽"}
    assert all(r["line"] == "investment" and r["currency"] for r in rows)


def test_parse_sinopac_respects_time_window():
    rows = parse_sinopac(SINOPAC, NOW)
    assert len(rows) == 19
    assert {"insurer": "友邦人壽", "product": "鍾愛一生美元利率變動型還本終身保險", "category": "美元利變還本",
            "currency": "", "line": "usd_interest"} in rows
    # 頁面用 JS 隱藏不在上下架期間內的項目；2018 年時這些項目都還沒上架
    assert parse_sinopac(SINOPAC, datetime(2018, 1, 1, tzinfo=TPE)) == []


def _mega_site(body: bytes):
    return FakeSite({MEGA_URL: (200, body, {})})


def _run(make_env, site, db_env=None):
    db, raw, http = db_env or make_env(site)
    ad = BankShelfAdapter(make_ctx(raw, http, validator_lookup(db)), CFG)
    return (db, raw, http), run_source(ad, db, refetch=True)


def _shelf(db):
    return {r["product"]: dict(r) for r in db.conn.execute("SELECT * FROM bank_shelf")}


def test_first_run_is_baseline_and_uses_post(make_env):
    site = _mega_site(MEGA)
    env, st = _run(make_env, site)
    assert st.new_items == 1 and st.errors == 0
    assert site.requests[-1].method == "POST" and b"itemID" in site.requests[-1].content
    shelf = _shelf(env[0])
    assert len(shelf) == 99 and all(r["baseline"] == 1 and r["removed_at"] is None for r in shelf.values())
    _, again = _run(make_env, site, env)
    assert again.unchanged == 1 and again.new == 0       # 內容相同不產生新版本


def test_launch_and_removal_detected(make_env):
    site = _mega_site(MEGA)
    env, _ = _run(make_env, site)
    items = json.loads(MEGA)
    gone = items.pop(0)["InsuranceName"]
    items.append({"InsuranceCompany": "安聯人壽", "InsuranceName": "安聯人壽美利旺分紅終身壽險",
                  "InsuranceType": "終身型保險", "Currency": "美元"})
    site.routes[MEGA_URL] = (200, json.dumps(items, ensure_ascii=False).encode(), {})
    _, st = _run(make_env, site, env)
    assert st.revised == 1
    shelf = _shelf(env[0])
    new = shelf["美利旺分紅終身壽險"]
    assert new["baseline"] == 0 and new["removed_at"] is None and new["line"] == "participating"
    removed = [r for r in shelf.values() if r["removed_at"]]
    assert len(removed) == 1 and gone.endswith(removed[0]["product"])
    # 重新出現 → 清掉 removed_at
    site.routes[MEGA_URL] = (200, MEGA, {})
    _run(make_env, site, env)
    assert _shelf(env[0])[removed[0]["product"]]["removed_at"] is None


@pytest.mark.parametrize("body", [b"[]", json.dumps(json.loads(MEGA)[:10], ensure_ascii=False).encode()])
def test_shrunk_or_empty_listing_does_not_mark_removals(make_env, body):
    site = _mega_site(MEGA)
    env, _ = _run(make_env, site)
    site.routes[MEGA_URL] = (200, body, {})
    _, st = _run(make_env, site, env)
    assert st.errors == 1 and st.new == 0
    assert all(r["removed_at"] is None for r in _shelf(env[0]).values())


def test_compat_ideograph_prefix_is_stripped():
    # 兆豐 API 的「法國巴黎人壽活力成家定期壽險」用了 U+F989（相容漢字「黎」）
    rows = parse_mega(MEGA)
    assert not [r["product"] for r in rows if "人壽" in r["product"][:6]]
    assert {"insurer": "法國巴黎人壽", "product": "活力成家定期壽險"}.items() <= next(
        r for r in rows if "活力成家" in r["product"]).items()
    assert norm_insurer("法國巴黎人壽") == "法國巴黎人壽"
    assert strip_insurer_keeps_fullwidth()


def strip_insurer_keeps_fullwidth():
    from adapters.bank_shelf import strip_insurer
    return strip_insurer("凱基人壽", "凱基人壽天天有利－定期給付型（甲型）") == "天天有利－定期給付型（甲型）"
