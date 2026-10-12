from parsers.gist import first_sentence, gist, law_item, shorten, subject_line


def test_subject_line_stops_at_next_heading():
    desc = ("金融監督管理委員會　公告<br />發文日期：中華民國115年10月6日<br />"
            "主旨：預告修正「保險業資金辦理專案運用公共及社會福利事業投資管理辦法」第二條、第七條草案。<br />"
            "依據：行政程序法第一百五十一條第二項。")
    assert subject_line(desc) == "預告修正「保險業資金辦理專案運用公共及社會福利事業投資管理辦法」第二條、第七條草案。"


def test_penalty_subject_does_not_run_into_facts():
    desc = "主旨：核處新臺幣960萬元罰鍰。 事實： 一、貴公司…"
    assert gist("fsc_penalty", desc) == "核處新臺幣960萬元罰鍰。"


def test_first_sentence_drops_alias_notes_and_uses_short_names():
    body = ("金融監督管理委員會(下稱金管會)近日通過對台灣人壽保險股份有限公司(下稱台灣人壽或該公司)處分案。"
            "台灣人壽為處理客戶反映未收到已失效保單之退費事宜，發現異常情事。")
    assert first_sentence(body).startswith("金管會近日通過對台灣人壽保險股份有限公司處分案。台灣人壽")


def test_short_lead_gets_next_sentence():
    lead = "壽險業未來獲利不能再各算各的。金管會昨（8）日召集全體業者研商CSM計算，目標降低各公司評估落差。"
    assert first_sentence(lead) == lead


def test_long_sentence_is_cut_at_comma():
    s = "甲" * 70 + "，" + "乙" * 70 + "。"
    assert shorten(s) == "甲" * 70 + "…"


def test_law_order_skips_legal_basis_item():
    page = ("選單 最新動態 全文內容：一、依據保險業各種準備金提存辦法第十八條規定辦 理。 "
            "二、財產保險業經營核能保險，應提存下列各種準備金： （一）未滿期保費準備金。 三、本令自即日生效。")
    assert gist("tii_law_rss", page) == "財產保險業經營核能保險，應提存下列各種準備金。"


def test_law_text_page_uses_amendment_line():
    page = ("資料類別：法律命令 115.10.05 保險業財務報告編製準則 中華民國一百十五年十月五日金融監督管理委員會"
            "金管保財字第 115 04935831 號令修正發布第 4、9 條條文 第 4 條 財務報告指財務報表")
    assert law_item(page) == "金管會修正發布第4、9條條文。"
    joint = ("中華民國一百十五年九月一日金融監督管理委員會金管保財字第 11504 號令、中央銀行台央外拾壹字第1150017266號令"
             "會銜修正發布第5、17條條文 第 5 條")
    assert law_item(joint) == "金管會、中央銀行會銜修正發布第5、17條條文。"
    assert law_item("中華民國一百十五年十月二日金融監督管理委員會金管保財字第 11501 410124 號令發布廢止；並自自即日生效") == "金管會發布廢止。"


def test_law_page_never_falls_back_to_menu_text():
    assert gist("tii_law_rss", "訊息內容 系統首頁 中心首頁 English Version 選擇項目") == ""
