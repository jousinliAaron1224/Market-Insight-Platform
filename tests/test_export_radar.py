from scripts.export_radar import mentioned, pct


def test_mentioned_finds_company_ids_once():
    assert mentioned("台灣人壽保險股份有限公司違反保險法令裁罰案") == ["taiwanlife"]
    assert mentioned("法國巴黎人壽與法巴人壽、國泰人壽") == ["cardif", "cathay"]
    assert mentioned("金管會提醒民眾投保登山險") == []


def test_pct_keeps_integers_clean():
    assert pct(None) is None
    assert pct(5.0) == 5
    assert pct(2.456) == 2.46
