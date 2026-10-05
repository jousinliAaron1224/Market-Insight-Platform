"""從各公司「契約條款」頁（HTML 或 PDF）建立「商品名稱 → 條款 PDF 網址」對照表。"""
from __future__ import annotations

import io
import re
import unicodedata
from urllib.parse import urljoin

import pdfplumber
from selectolax.parser import HTMLParser


def norm_name(s: str) -> str:
    """比對用的名稱：NFKC、去空白、去掉連結文字尾巴的「pdf」「(另開新視窗)」等。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(?i)(pdf|在新標籤中開啟|\(另開新視窗\)|另開新視窗)$", "", s)
    return s


def from_html(html: bytes | str, base_url: str, pattern: str = r"變額") -> dict[str, str]:
    text = html if isinstance(html, str) else html.decode("utf-8", "replace")
    out: dict[str, str] = {}
    for a in HTMLParser(text).css("a"):
        name = norm_name(a.text(strip=True))
        href = a.attributes.get("href") or ""
        if not name or not href or href.startswith("javascript") or not re.search(pattern, name):
            continue
        out.setdefault(name, urljoin(base_url, href))
    return out


def from_pdf_hyperlinks(data: bytes, pattern: str = r"變額") -> dict[str, str]:
    """台灣人壽：條款清單是 PDF，每列右側「契約條款下載」是超連結；以垂直位置對應到左側商品名稱。

    列距只有約 13.5pt，不能用「連結上下放寬幾 pt」去抓文字，否則會把上下相鄰兩列的名稱也併進來，
    再經過 match() 的前綴比對就對到隔壁列的批註條款（2026-10-04 發現：10 個商品對錯）。
    做法：左側文字先依行分組，每一行歸給垂直重疊最多的那個連結；沒有重疊任何連結的行視為上一行的折行。
    """
    out: dict[str, str] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            links = sorted((h for h in page.hyperlinks if h.get("uri")), key=lambda h: h["top"])
            if not links:
                continue
            left_edge = min(h["x0"] for h in links)
            words = [w for w in page.extract_words(keep_blank_chars=False) if w["x1"] < left_edge]
            rows: list[list[dict]] = []
            for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
                if rows and abs(rows[-1][0]["top"] - w["top"]) < 3:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            names: dict[int, list[str]] = {}
            last: int | None = None
            for row in rows:
                top, bottom = min(w["top"] for w in row), max(w["bottom"] for w in row)
                overlaps = [(min(bottom, h["bottom"]) - max(top, h["top"]), i) for i, h in enumerate(links)]
                best, i = max(overlaps)
                if best > 0:
                    last = i
                elif last is None:
                    continue                        # 表頭等連結以上的文字
                names.setdefault(last if best <= 0 else i, []).append(
                    "".join(w["text"] for w in sorted(row, key=lambda w: w["x0"])))
            for i, parts in names.items():
                name = norm_name("".join(parts)).replace("契約條款下載", "")
                if name and re.search(pattern, name):
                    out.setdefault(name, links[i]["uri"])
    return out


def match(name: str, index: dict[str, str]) -> str | None:
    """先完全比對，再容許一方是另一方的前綴（例如清單名稱多了「(112)」版本字樣）。"""
    n = norm_name(name)
    if n in index:
        return index[n]
    for k, v in index.items():
        if k.startswith(n) or n.startswith(k):
            return v
    return None
