"""一行主旨（gist）：從公告或新聞內文以規則取出一句話，給雷達網站的情報列表與詳細頁顯示。不用 LLM。

規則：
1. 內文有「主旨：」就用主旨那一行（金管會裁罰、法規草案預告、函釋）。
2. 保發中心法規頁（tii_law_rss）：頁面前面是網站選單，不取第一句。
   - 有「全文內容」（令、函釋）：條列第一點通常只是「依據…辦理」，取第一個不是「依據」的條列。
   - 法規條文頁（辦法、準則、規則）：用異動說明，例如「金管會修正發布第 4、9 條條文」「金管會發布廢止」。
3. 其他（金管會新聞稿內文、新聞 RSS 導言）：取第一句；第一句太短（新聞常見的引言）就接下一句。
太長的句子在 max_len 前最後一個逗號處截斷並加「…」。取不到就回傳空字串，網站不顯示。
"""
from __future__ import annotations

import html
import re

MAX_LEN = 120
MIN_LEN = 25
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_CJK = r"一-鿿"
_SUBJECT = re.compile(r"主\s*旨\s*[：:]\s*(.+?)(?=(?:依\s*據|說\s*明|公告事項|附\s*件|事\s*實|理\s*由|法令依據)\s*[：:]|$)")
_ITEM = re.compile(r"(?:^|\s|。)([一二三四五六七八九十]{1,3})、")
# 「（以下簡稱金管會）」「(下稱台灣人壽或該公司)」這類簡稱註記直接拿掉，機關全名再換成常用簡稱
_ALIAS = re.compile(r"[（(](?:以下|下)?(?:統|簡)?稱[^）)]{1,30}[）)]")
_SHORT = {"金融監督管理委員會": "金管會", "財團法人保險事業發展中心": "保發中心"}
_ISSUER = re.compile(r"金融監督管理委員會|中央銀行|交通部|財政部|衛生福利部|內政部|經濟部|勞動部|法務部")
# 法規條文頁的異動說明：「中華民國…日金融監督管理委員會金管保財字第 115 04935831 號令修正發布第 4、9 條條文；…」
_AMEND = re.compile(r"中華民國[^。]{4,40}?日(.{2,60}?)字第[\d\s]+號令(.+?)(?:；|。|本系統由|$)")


def clean(text: str) -> str:
    """HTML 轉純文字、合併空白。中文在詞中間斷行（法規全文常見），兩個中文字之間的空白直接拿掉。"""
    t = html.unescape(_TAG.sub(" ", text or ""))
    t = re.sub(rf"(?<=[{_CJK}，、；（）])\s+(?=[{_CJK}，、；（）])", "", t)
    return _SPACE.sub(" ", t).strip()


def shorten(s: str, max_len: int = MAX_LEN) -> str:
    s = s.strip().rstrip("。；;，, ")
    if not s:
        return ""
    if len(s) <= max_len:
        return s + "。"
    cut = max(s.rfind("，", 0, max_len), s.rfind("、", 0, max_len), s.rfind("；", 0, max_len))
    return (s[:cut] if cut >= max_len // 2 else s[:max_len]).rstrip("，、；") + "…"


def short_names(t: str) -> str:
    t = _ALIAS.sub("", t)
    for full, short in _SHORT.items():
        t = t.replace(full, short)
    return t


def first_sentence(text: str, max_len: int = MAX_LEN) -> str:
    t = short_names(clean(text))
    sents = [x for x in re.findall(r"[^。！？]+[。！？]?", t) if x.strip()]
    if not sents:
        return ""
    out = sents[0]
    if len(out) < MIN_LEN and len(sents) > 1:
        out += sents[1]
    return shorten(out, max_len)


def subject_line(text: str, max_len: int = MAX_LEN) -> str:
    m = _SUBJECT.search(clean(text))
    return shorten(short_names(m.group(1)), max_len) if m else ""


def law_item(text: str, max_len: int = MAX_LEN) -> str:
    """保發中心法規頁：令／函釋取第一個實質條列；法規條文頁取異動說明。"""
    t = clean(text)
    i = t.find("全文內容")
    if i >= 0:
        body_all = t[i + len("全文內容"):].lstrip("：: ")
        starts = [m.start(1) for m in _ITEM.finditer(" " + body_all)]
        for a, b in zip(starts, starts[1:] + [len(body_all) + 1]):
            body = re.sub(r"^[一二三四五六七八九十]{1,3}、", "", body_all[a - 1:b - 1]).strip()
            if body and not body.startswith("依據") and not re.match(r"本(令|函|規定)自", body):
                body = re.split(r"[：:](?=\s*[（(][一二三四五六七八九十])", body)[0]   # 「應提存下列各種準備金：（一）…」只留前半句
                return first_sentence(body, max_len)
        if not starts:
            return first_sentence(body_all, max_len)
    m = _AMEND.search(t)
    if m:
        who = "、".join(dict.fromkeys(_SHORT.get(x, x) for x in _ISSUER.findall(m.group(1) + m.group(2)[:30]))) or "主管機關"
        act = re.sub(r"\s+", "", m.group(2)).replace("並自自", "並自")
        act = re.split(r"(?<=條文)(?=第\d)", act)[0]   # 後面接著是條文本文，不要
        act = re.sub(r"^、?[\u4e00-\u9fff]{0,12}字第[\d\s]+號令", "", act)   # 會銜發布時第二個機關和它的發文字號
        return shorten(who + act, max_len)
    return ""


def gist(source_id: str, text: str, max_len: int = MAX_LEN) -> str:
    if not text:
        return ""
    if source_id == "tii_law_rss":
        return subject_line(text, max_len) or law_item(text, max_len)
    return subject_line(text, max_len) or first_sentence(text, max_len)
