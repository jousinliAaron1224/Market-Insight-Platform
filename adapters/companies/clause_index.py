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
    """台灣人壽：條款清單是 PDF，每列右側「契約條款下載」是超連結；以垂直位置對應到左側商品名稱。"""
    out: dict[str, str] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            links = [h for h in page.hyperlinks if h.get("uri")]
            if not links:
                continue
            words = page.extract_words(keep_blank_chars=False)
            for h in links:
                top, bottom = h["top"], h["bottom"]
                # 名稱可能折成兩行：取與連結垂直重疊（上下放寬 6pt）且位於連結左側的文字
                row = [w for w in words if w["x1"] < h["x0"] and w["bottom"] > top - 6 and w["top"] < bottom + 6]
                name = norm_name("".join(w["text"] for w in sorted(row, key=lambda w: (round(w["top"]), w["x0"]))))
                name = name.replace("契約條款下載", "")
                if name and re.search(pattern, name):
                    out.setdefault(name, h["uri"])
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
