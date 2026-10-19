from parsers.open_data import parse_jcic_loan_age, parse_jcic_mortgage_age, parse_jcic_mortgage_gender, parse_ndc_population


def test_jcic_mortgage_age():
    data = '﻿"年","月","借款人年齡區間","人數","授信餘額[仟元]","平均利率[%]"\n2026,06,"30-40",7193,71359022,2.3527665857\n'.encode()
    assert parse_jcic_mortgage_age(data) == [{"m": "2026-06", "age": "30-40", "n": 7193, "amt": 71359.0, "rate": 2.353}]


def test_jcic_mortgage_gender_maps_sex():
    head = "﻿年,月,性別,樣本數,平均授信餘額[千元],平均授信額度[千元],平均鑑估值 [千元],核貸成數[%]-算術平均,核貸成數[%]-中位數,核貸成數[%]-加權平均,平均貸款利率[%],平均建物面積[平方公尺]\n"
    r = parse_jcic_mortgage_gender((head + "2026,6,2,18604,10304,10518,14864,71.33,75.71,70.76,2.44,135\n").encode())[0]
    assert r["q"] == "2026-06" and r["sex"] == "女" and r["limit"] == 10518 and r["ltv"] == 70.76 and r["rate"] == 2.44


def test_jcic_loan_age_cleans_type():
    data = "﻿年,月,性別,年齡,授信類別,人數,額度,件數,平均每筆額度,餘額,逾期放款比率\n2025,12,1,65以上,1.房貸(含修繕),62657,546836,67144,8.14,283351,1.5E-03\n".encode()
    r = parse_jcic_loan_age(data)[0]
    assert r["type"] == "房貸" and r["age"] == "65以上" and r["n"] == 62657 and r["sex"] == "男"


def test_ndc_population():
    data = ("年,地區,土地面積,年底人口數男性,年底人口數女性,年底人口數,年底戶數,戶量,人口密度,外僑居住人數,年齡0~14歲,年齡0~14歲百分比,年齡15~64歲,年齡15~64歲百分比,年齡65歲以上,年齡65歲以上百分比,扶養百分比\n"
            "2024,臺北市,271.8,1174537,1316332,2490869,1071493,2.32,9164.3,79604,303025,12.166,1613386,64.772,574458,23.0626,54.39\n").encode()
    r = parse_ndc_population(data)[0]
    assert r == {"y": 2024, "area": "臺北市", "pop": 2490869, "households": 1071493, "p0_14": 12.17, "p15_64": 64.77, "p65": 23.06, "dep": 54.39}
