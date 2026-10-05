"""條款 PDF → 條文結構與統一 schema 欄位（純規則，不依賴 LLM；M4，D22）。

步驟：
1. 逐頁取出文字行。偵測雙欄排版（例如法巴：頁面中線有一條沒有字的縫），雙欄時每頁
   依「整列跨欄的行」切段，每段先讀左欄再讀右欄。
2. 去掉頁首頁尾：位在頁面上下緣、且（數字換成 # 之後）在一半以上頁面重複出現的行，
   例如「第 1 頁，共 21 頁」「UOPX1140701 1/19」「【BNAGSVA】-1/14-」。
3. 切條文：行首「第 N 條」且條號連續（避免把內文換行到行首的「第十條約定…」誤判）。
   條文標題可能在同一行（國泰）、前一行的【】（富邦、凱基、台灣人壽）或前一行的短標題（法巴）。
   第一條之前是前言（商品名稱、給付項目、備查文號），最後一條之後以「附表／附件」開頭的是附表。
4. 欄位：只抽條款裡明確寫出的內容，每個欄位都記出處（條號、標題、頁碼）；
   抽不到的列入 missing，留給之後的 LLM 補強（parsers.base.Enricher）。
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import pdfplumber

PARSER_NAME = "clause-rules-v1"
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(s: str) -> int | None:
    """'二十一' -> 21；'一百零二' -> 102；阿拉伯數字直接轉。"""
    s = s.strip().replace(" ", "")
    if s.isdigit():
        return int(s)
    if not s or any(ch not in CN_DIGITS and ch not in "十百" for ch in s):
        return None
    total, num = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
    return total + num


# ---------------------------------------------------------------- 1. 取行
@dataclass
class Line:
    page: int          # 1-based
    top: float
    text: str


def _chars_text(chars: list[dict]) -> str:
    chars = sorted(chars, key=lambda c: c["x0"])
    out, prev = [], None
    for c in chars:
        if prev is not None and c["x0"] - prev["x1"] > max(c.get("size", 10), 6) * 0.6:
            out.append(" ")
        out.append(c["text"])
        prev = c
    # NFKC：有些 PDF 用康熙部首（⽉ ⽇ ⾦）或全形數字，正規化後規則才比對得到
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", "".join(out))).strip()


def _column_gap(page) -> tuple[float, float] | None:
    """頁面中段（0.38–0.62 寬）最長的無字縱帶；寬度 ≥ 8pt 視為雙欄的欄縫。"""
    W, H = float(page.width), float(page.height)
    occ = [0] * (int(W) + 1)
    for c in page.chars:
        if not c["text"].strip() or c["top"] < H * 0.07 or c["bottom"] > H * 0.93:
            continue
        for x in range(max(0, int(c["x0"])), min(len(occ), int(c["x1"]) + 1)):
            occ[x] += 1
    best, run = (0, 0), None
    for x in range(int(W * 0.38), int(W * 0.62)):
        if occ[x] == 0:
            run = x if run is None else run
            if x + 1 - run > best[1] - best[0]:
                best = (run, x + 1)
        else:
            run = None
    if best[1] - best[0] < 8:
        return None
    # 欄縫兩側都要有足夠的字，否則只是版面留白
    left = sum(occ[: best[0]])
    right = sum(occ[best[1]:])
    return (best[0], best[1]) if left > 50 and right > 50 else None


def _page_lines(page, pno: int, gap: tuple[float, float] | None) -> list[Line]:
    raw = page.extract_text_lines(layout=False, strip=True, return_chars=True)
    if not gap:
        return [Line(pno, ln["top"], _chars_text(ln["chars"])) for ln in raw if ln["chars"]]
    mid = (gap[0] + gap[1]) / 2
    out: list[Line] = []
    block_l: list[Line] = []
    block_r: list[Line] = []

    def flush():
        out.extend(block_l)
        out.extend(block_r)
        block_l.clear()
        block_r.clear()

    for ln in raw:
        chars = [c for c in ln["chars"] if c["text"].strip()]
        if not chars:
            continue
        # 跨欄＝有字壓在欄縫中線上（首頁沿用其他頁的欄縫時，左欄的字可能略為伸進欄縫，不算跨欄）
        crosses = any(c["x0"] < mid - 1 and c["x1"] > mid + 1 for c in chars)
        if crosses:  # 跨欄的整列（標題、頁尾）：先把目前段落的左右欄輸出
            flush()
            out.append(Line(pno, ln["top"], _chars_text(chars)))
            continue
        lc = [c for c in chars if c["x1"] <= mid]
        rc = [c for c in chars if c["x0"] >= mid]
        if lc:
            block_l.append(Line(pno, ln["top"], _chars_text(lc)))
        if rc:
            block_r.append(Line(pno, ln["top"], _chars_text(rc)))
    flush()
    return out


_NUM = re.compile(r"\d+")


def _strip_headers_footers(pages: list[list[Line]], heights: list[float]) -> tuple[list[Line], int]:
    n = len(pages)
    if n < 3:
        lines = [ln for p in pages for ln in p]
    else:
        def zone(ln: Line) -> bool:
            h = heights[ln.page - 1]
            return ln.top < h * 0.09 or ln.top > h * 0.90
        counts: Counter[str] = Counter()
        for p in pages:
            counts.update({_NUM.sub("#", ln.text) for ln in p if zone(ln)})
        repeated = {k for k, v in counts.items() if v >= max(3, n * 0.5)}
        lines = [ln for p in pages for ln in p if not (zone(ln) and _NUM.sub("#", ln.text) in repeated)]
    before = len(lines)
    page_no = re.compile(r"^(第\s*\d+\s*頁\s*[，,]?\s*共\s*\d+\s*頁|-?\s*\d+\s*/\s*\d+\s*-?|-\s*\d+\s*-|\d+)$")
    lines = [ln for ln in lines if ln.text and not page_no.match(ln.text)]
    return lines, before - len(lines)


def extract_lines(pdf_bytes: bytes) -> tuple[list[Line], dict[str, Any]]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        gaps = [_column_gap(p) for p in pdf.pages]
        two_col_pages = sum(g is not None for g in gaps)
        two_col = two_col_pages >= max(1, len(gaps) * 0.5)
        # 首頁（跨欄的商品名稱與文號）或附表頁偵測不到欄縫時，沿用全文件最常見的欄縫；
        # 跨過欄縫的行會被當成整列，不會被切開
        found = [g for g in gaps if g]
        common = Counter(found).most_common(1)[0][0] if found else None
        pages = [_page_lines(p, i + 1, (gaps[i] or common) if two_col else None)
                 for i, p in enumerate(pdf.pages)]
        heights = [float(p.height) for p in pdf.pages]
    lines, dropped = _strip_headers_footers(pages, heights)
    return lines, {"pages": len(pages), "two_column": two_col, "dropped_header_lines": dropped}


# ---------------------------------------------------------------- 2. 切條文
@dataclass
class Section:
    kind: str                 # preamble | article | appendix
    article_no: int | None
    title: str | None
    lines: list[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def flat(self) -> str:
        """去掉換行與空白的本文：PDF 換行常把詞切開，比對與抽欄位用這個。"""
        return re.sub(r"\s+", "", self.text)

    def as_row(self, seq: int) -> dict[str, Any]:
        pages = [ln.page for ln in self.lines] or [None]
        return {"seq": seq, "kind": self.kind, "article_no": self.article_no, "title": self.title,
                "text": self.text, "page_start": min(p for p in pages if p) if any(pages) else None,
                "page_end": max(p for p in pages if p) if any(pages) else None,
                "text_hash": hashlib.sha256(self.flat.encode("utf-8")).hexdigest()}


HEAD = re.compile(r"^第\s*([一二三四五六七八九十百零〇\d ]{1,8}?)\s*條(?:\s*[:：])?\s*(.*)$")
BRACKET = re.compile(r"^[【\[](.{1,45})[】\]]$")
PUNCT = re.compile(r"[，。；：,;:]")
APPENDIX = re.compile(r"^(附表|附件)\s*[一二三四五六七八九十\d]*")


FILING_LABEL = re.compile(r"^(?:備查|逕修|修訂|核准|修正|核備)(?:日期)?(?:及)?文號\s*[:：]")
FILING_CONT = re.compile(r"第\s*\d{5,}\s*號")


def _short_title(s: str) -> bool:
    """條文標題：短、沒有句讀（可有頓號與括號，例如「契約的終止（一）」）。"""
    return (0 < len(s) <= 40 and not PUNCT.search(s) and not re.match(r"^[一二三四五六七八九十]+、", s)
            and not re.match(r"^[(（][一二三四五六七八九十\d]+[)）]", s))


# 行首的「第十九條約定辦理」是交互引用，不是條文開頭
XREF = re.compile(r"^(約定|規定|之|所|及|或|、|,|，|。|至|的|辦理|項|款|但|後|前|起|者|"
                  r"第[一二三四五六七八九十百\d]+[條項款])")


def split_sections(lines: list[Line]) -> tuple[list[Section], list[str]]:
    warnings: list[str] = []
    sections = [Section("preamble", None, None)]
    last_no = 0
    in_filing = False
    for ln in lines:
        # 文號區塊（法巴放在首頁右欄，讀取順序會落在條文中間）一律歸前言，不混進條文
        if FILING_LABEL.match(ln.text) or (in_filing and FILING_CONT.search(ln.text)):
            in_filing = True
            sections[0].lines.append(ln)
            continue
        in_filing = False
        m = HEAD.match(ln.text)
        no = cn_number(m.group(1)) if m else None
        if (m and no is not None and (no == last_no + 1 or (last_no and no == last_no + 2))
                and not XREF.match(m.group(2).strip())):
            if no == last_no + 2:
                warnings.append(f"條號跳號：第{last_no}條之後是第{no}條")
            cur = sections[-1]
            rest = m.group(2).strip()
            title = None
            body_first = None
            if rest and _short_title(rest):
                title = rest                                     # 國泰：第一條 保險契約的構成
            else:
                body_first = rest or None
            if title is None and cur.lines:                      # 前一行是標題
                prev = cur.lines[-1].text
                b = BRACKET.match(prev)
                if b:
                    title = b.group(1).strip()
                    cur.lines.pop()
                elif _short_title(prev) and len(cur.lines) > 1 and cur.kind == "article":
                    title = prev                                 # 法巴：標題獨立一行、無括號
                    cur.lines.pop()
                elif _short_title(prev) and cur.kind == "preamble" and no == 1:
                    title = prev
                    cur.lines.pop()
            sec = Section("article", no, title)
            if body_first:
                sec.lines.append(Line(ln.page, ln.top, body_first))
            sections.append(sec)
            last_no = no
            continue
        sections[-1].lines.append(ln)

    # 最後一條之後的附表／附件
    last = sections[-1]
    if last.kind == "article":
        for i, ln in enumerate(last.lines):
            if APPENDIX.match(ln.text) and "。" not in ln.text and len(ln.text) <= 40 and i > 0:
                appendix = Section("appendix", None, ln.text[:40], last.lines[i:])
                last.lines = last.lines[:i]
                sections.append(appendix)
                break
    if last_no == 0:
        warnings.append("找不到任何條文（第一條）")
    return sections, warnings


# ---------------------------------------------------------------- 3. 欄位
FIELDS = ["coverage", "benefit_articles", "exclusions", "payment_modes", "currency", "issue_age",
          "rate_terms", "riders", "accumulation_min_years", "free_look_days", "participating",
          "filings", "appendices"]
REQUIRED = ["coverage", "exclusions", "payment_modes", "currency", "issue_age", "rate_terms", "riders"]
CURRENCY = {"新臺幣": "TWD", "新台幣": "TWD", "美元": "USD", "澳幣": "AUD", "人民幣": "CNY",
            "歐元": "EUR", "日圓": "JPY", "日幣": "JPY", "英鎊": "GBP", "加幣": "CAD", "紐幣": "NZD",
            "港幣": "HKD", "南非幣": "ZAR"}
_CUR_RX = "|".join(CURRENCY)
# 繳費方式的各種寫法 → 統一名稱
PAY_PATTERNS = [(r"躉繳|一次(?:繳|交付)", "躉繳"), (r"彈性(?:繳|交付)|彈性或分期", "彈性繳"),
                (r"分期(?:繳|交付)|彈性或分期", "分期繳"), (r"(?<!不)定期繳", "定期繳"),
                (r"不定期(?:繳|方式|保險費)", "不定期繳"), (r"(?<!半)年繳", "年繳"), (r"半年繳", "半年繳"),
                (r"季繳", "季繳"), (r"月繳", "月繳")]
_DATE = re.compile(r"(?:民國|中華民國)?\s*(\d{2,4})\s*[.年/]\s*(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*日?")


def _ev(sec: Section, **extra) -> dict[str, Any]:
    row = sec.as_row(0)
    return {"article_no": sec.article_no, "title": sec.title, "page": row["page_start"], **extra}


def _date_iso(m: re.Match) -> str | None:
    y, mo, d = (int(x) for x in m.groups())
    if y < 1911:
        y += 1911
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


_DOCNO = re.compile(r"((?:[\u4e00-\u9fff]|[(（]\d{2,3}[)）]){1,8}字第\s*\d+\s*號)")
_KINDS = ("核准", "修正", "修訂", "逕修", "備查")


_SUFFIX = re.compile(r"^[函令](?:暨[^。]{0,30}?號[函令]?)?(備查|修正|核准|核備)")
_LABEL = re.compile(r"(核准|修正|修訂|逕修|備查|核備)(?:日期)?及?文號")


def parse_filings(preamble: str) -> list[dict[str, Any]]:
    """前言的備查／核准／修正文號。一筆可能跨行，所以換行接起來（保留空白當分隔）再以文號切段。
    種類優先用文號前的標籤（「逕修文號：」），沒有標籤才用文號後的「函備查／令修正」。"""
    flat = re.sub(r"\s*\n\s*", "", preamble)
    out, start = [], 0
    for m in _DOCNO.finditer(flat):
        seg = flat[start:m.start()]
        sm = _SUFFIX.match(flat[m.end():m.end() + 40])
        start = m.end() + (sm.end() if sm else 0)
        doc_no = re.sub(r"^.*[年月日依]", "", m.group(1))    # 「…30日金管保壽字…」只留發文機關字號
        dates = [d for d in (_date_iso(x) for x in _DATE.finditer(seg + m.group(1))) if d]
        labels = _LABEL.findall(seg)
        kind = labels[-1] if labels else (sm.group(1) if sm else None)
        if out and not dates and not labels and out[-1]["date"]:
            dates = [out[-1]["date"]]       # 「…號函暨…號函修正」：同一筆的第二個文號
        out.append({"date": dates[0] if dates else None, "doc_no": doc_no, "kind": kind})
    return out


def parse_benefits(preamble: Section) -> list[str]:
    """前言的給付項目。三種寫法：
    - 同一行或括號跨行：（給付項目：祝壽保險金、身故保險金…）／【給付項目：…、\n完全失能保險金】
    - 台灣人壽：主要給付項目：（同行是日期）\n1.年金給付\n2.返還保單帳戶價值
    - 凱基：第二行整行是 (返還保單帳戶價值、年金給付)
    """
    lines = [ln.text for ln in preamble.lines]
    items: list[str] = []
    for i, t in enumerate(lines):
        m = re.search(r"([(（【\[]?)\s*(?:主要)?給付項目\s*[:：]\s*(.*)$", t)
        if not m:
            continue
        rest = m.group(2).strip()
        if m.group(1):  # 有開括號：一直接到閉括號（最多 3 行）
            j = i
            while not re.search(r"[)）】\]]", rest) and j + 1 < len(lines) and j < i + 3:
                j += 1
                rest += lines[j].strip()
            rest = re.split(r"[，,；;]", rest)[0]     # 逗號後面是附註（例如「年金最高給付至…」）
            rest = re.split(r"[)）】\]]", rest)[0]
        if rest and not _DATE.match(rest):
            items = [x.strip() for x in re.split(r"[、，,；;]", rest) if x.strip()]
        else:   # 下面幾行是 1.xxx 2.xxx（同一行右側可能夾著文號，只取第一段）
            for nxt in lines[i + 1:i + 12]:
                mm = re.match(r"^\d+\s*[.、．]\s*(\S+)", nxt)
                if mm:
                    items.append(mm.group(1))
                elif items:
                    break
        break
    if not items:
        for t in lines[:6]:
            mm = re.match(r"^[(（【]([^()（）【】]+、[^()（）【】]+)[)）】]$", t)
            if mm and "分紅" not in mm.group(1):
                items = [x.strip() for x in mm.group(1).split("、") if x.strip()]
                break
    return [re.sub(r"[。.]$", "", x) for x in items if len(x) <= 40]


def extract_fields(sections: list[Section], product_name: str | None = None) -> dict[str, Any]:
    pre = sections[0]
    arts = [s for s in sections if s.kind == "article"]
    fields: dict[str, Any] = {}
    evidence: dict[str, Any] = {}

    def first(pattern: str, secs=None, flags=0):
        rx = re.compile(pattern, flags)
        for s in secs if secs is not None else arts:
            m = rx.search(s.flat)
            if m:
                return s, m
        return None, None

    # 給付項目（保障範圍）
    ben = parse_benefits(pre)
    if ben:
        fields["coverage"] = ben
        evidence["coverage"] = {"article_no": None, "title": "條款前言", "page": pre.as_row(0)["page_start"]}
    benefit_arts = [s for s in arts if s.title and re.search(r"保險金|給付|保險範圍|返還", s.title)]
    if benefit_arts:
        fields["benefit_articles"] = [{"article_no": s.article_no, "title": s.title} for s in benefit_arts]
        if "coverage" not in fields:
            fields["coverage"] = [s.title for s in benefit_arts]
            evidence["coverage"] = _ev(benefit_arts[0])

    # 除外責任
    exc = [s for s in arts if s.title and "除外責任" in s.title]
    if not exc:
        exc = [s for s in arts if not s.title and "不負給付" in s.flat[:80]]
    if exc:
        fields["exclusions"] = re.sub(r"\s+", "", exc[0].text)[:200]
        evidence["exclusions"] = _ev(exc[0])

    # 繳費方式
    pays: list[str] = []
    pay_ev = None
    for s in arts:
        hits = [name for rx, name in PAY_PATTERNS if re.search(rx, s.flat)]
        if hits and pay_ev is None:
            pay_ev = s
        pays += [t for t in hits if t not in pays]
    if pays:
        fields["payment_modes"] = pays
        evidence["payment_modes"] = _ev(pay_ev)

    # 幣別：條款的「貨幣單位」為準（約定外幣型列出可選幣別），抓不到才用商品名稱
    money_first = sorted(arts, key=lambda x: 0 if x.title and "貨幣" in x.title else 1)
    s, m = first(r"可供選擇之約定外幣為[:：]?([^。]{2,80})", money_first)
    if s:
        codes = list(dict.fromkeys(CURRENCY[k] for k in re.findall(_CUR_RX, m.group(1))))
        if codes:
            fields["currency"] = "/".join(codes)
            evidence["currency"] = _ev(s, quote=m.group(0)[:60])
    if "currency" not in fields:
        s, m = first(rf"以({_CUR_RX})為(?:貨幣)?單位", money_first)
        if s:
            fields["currency"] = CURRENCY[m.group(1)]
            evidence["currency"] = _ev(s, quote=m.group(0))
    if "currency" not in fields and product_name:
        hit = next((v for k, v in CURRENCY.items() if k in product_name), None)
        fields["currency"] = hit or ("FX" if "外幣" in product_name else "TWD")
        evidence["currency"] = {"article_no": None, "title": "商品名稱", "page": None}

    # 投保年齡（多半寫在要保規則、不在條款；抓得到才填）
    s, m = first(r"(?:投保年齡|被保險人之?(?:投保)?年齡)[^。]{0,20}?(\d{1,2}|[零一二三四五六七八九十]+)\s*歲[^。]{0,12}?"
                 r"(?:至|~|～|－|-|到)\s*(\d{1,3}|[零一二三四五六七八九十百]+)\s*歲")
    if s:
        fields["issue_age"] = f"{cn_number(m.group(1))}–{cn_number(m.group(2))} 歲"
        evidence["issue_age"] = _ev(s, quote=m.group(0))

    # 利率（投資型條款通常只有定義，沒有數字）
    rates = {}
    for key in ("宣告利率", "預定利率"):
        s, m = first(rf"[一二三四五六七八九十]+、[「]?[^：:。]{{0,6}}{key}[」]?[：:]係指([^。]{{0,120}})")
        if s:
            rates[key] = m.group(1)
            evidence.setdefault("rate_terms", _ev(s))
    if rates:
        fields["rate_terms"] = rates

    # 附約
    s, m = first(r"附加[^。]{0,12}附約|附約[^。]{0,10}(?:附加|保險成本)")
    if s:
        fields["riders"] = "條款提及可附加附約"
        evidence["riders"] = _ev(s, quote=m.group(0))

    s, m = first(r"年金累積期間[^。]{0,40}?不得(?:低|少)於([一二三四五六七八九十\d]+)年")
    if s:
        fields["accumulation_min_years"] = cn_number(m.group(1))
        evidence["accumulation_min_years"] = _ev(s, quote=m.group(0))
    s, m = first(r"(?:送達|收到)[^。]{0,15}?翌日起(?:算)?([一二三四五六七八九十\d]+)日內")
    if s:
        fields["free_look_days"] = cn_number(m.group(1))
        evidence["free_look_days"] = _ev(s, quote=m.group(0))

    pre_flat = pre.flat
    if "不分紅" in pre_flat:
        fields["participating"] = False
    elif "分紅保險單" in pre_flat:
        fields["participating"] = True
    filings = parse_filings(pre.text)
    if filings:
        fields["filings"] = filings
        dated = [f["date"] for f in filings if f["date"]]
        if dated:
            fields["latest_filing_date"] = max(dated)
    apps = [s.title for s in sections if s.kind == "appendix"]
    if apps:
        fields["appendices"] = apps
    missing = [k for k in REQUIRED if k not in fields]
    return {"fields": fields, "evidence": evidence, "missing": missing}


# ---------------------------------------------------------------- 入口
@dataclass
class ClauseParse:
    pages: int
    two_column: bool
    sections: list[Section]
    fields: dict[str, Any]
    evidence: dict[str, Any]
    missing: list[str]
    warnings: list[str]
    not_main_clause: bool = False   # 條款對應錯了：拿到的是批註條款／附約，不是主約

    @property
    def articles(self) -> list[Section]:
        return [s for s in self.sections if s.kind == "article"]

    def rows(self) -> list[dict[str, Any]]:
        return [s.as_row(i) for i, s in enumerate(self.sections)]


def parse_clause_pdf(pdf_bytes: bytes, product_name: str | None = None) -> ClauseParse:
    lines, info = extract_lines(pdf_bytes)
    sections, warnings = split_sections(lines)
    if len([s for s in sections if s.kind == "article"]) < 5:
        warnings.append(f"條文數過少（{len([s for s in sections if s.kind == 'article'])}），可能不是條款或版面無法解析")
    # 條款對應檢查：文件抬頭是「…批註條款」或「…附約」，但商品名稱不是 → 抓錯檔（M3 clause_index 對應問題）
    head = next((ln.text for ln in sections[0].lines[:6] if re.search(r"批註條款|附約$|附加條款", ln.text)), None)
    not_main = bool(head) and not (product_name and re.search(r"批註|附約|附加條款", product_name))
    if not_main:
        warnings.append(f"非主約條款：文件抬頭為「{head}」，條款對應可能有誤")
        return ClauseParse(info["pages"], info["two_column"], sections, {}, {}, list(REQUIRED), warnings, True)
    ex = extract_fields(sections, product_name)
    return ClauseParse(info["pages"], info["two_column"], sections, ex["fields"], ex["evidence"],
                       ex["missing"], warnings)


def article_diff(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """以條號比對兩版條文：新增、刪除、內容改變（忽略空白與換行）。"""
    o = {r["article_no"]: r for r in old_rows if r["kind"] == "article"}
    n = {r["article_no"]: r for r in new_rows if r["kind"] == "article"}
    changed = [{"article_no": k, "title": n[k]["title"] or o[k]["title"]}
               for k in sorted(set(o) & set(n)) if o[k]["text_hash"] != n[k]["text_hash"]]
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)), "changed": changed}
