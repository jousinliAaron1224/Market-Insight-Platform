"""解析各壽險公司依《人身保險業辦理資訊公開管理辦法》公開的
「保險商品名稱、核准／核備／備查日期及文號」PDF 清單（M3，決策 D19）。

各家版面不同，但都是兩欄概念的表格：左欄商品名稱，右欄（一或兩欄）日期與文號。
名稱與文號常跨多個表格列，因此不靠儲存格，而是用「橫跨商品名稱欄的水平格線」切出商品區塊，
再分別擷取區塊內名稱欄與其他欄的文字。
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

import pdfplumber

ROC_DATE = re.compile(r"(?:民國|中華民國)?\s*(\d{2,3})\s*[年./]\s*(\d{1,2})\s*[月./]\s*(\d{1,2})\s*日?")
DOC_NO = re.compile(r"([一-鿿()（）\d]{1,12}字第\s*[A-Z0-9]+\s*號)")
INVESTMENT = re.compile(r"變額|投資型")
RIDER = re.compile(r"批註條款|附約|附加條款")
CATEGORY_WORDS = {"人壽保險", "年金保險", "健康保險", "傷害保險", "投資型保險", "團體保險", "微型保險",
                  "投資型商品", "傳統型個人險商品", "變額壽險", "變額年金保險", "變額萬能壽險"}


@dataclass
class ProductRow:
    name: str
    approvals: list[str] = field(default_factory=list)   # 原文的日期文號行
    first_date: str | None = None                        # 最早日期（ISO）
    latest_date: str | None = None                       # 最新日期（ISO，通常是最近一次修正）
    doc_numbers: list[str] = field(default_factory=list)
    is_investment: bool = False
    is_rider: bool = False
    page: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _iso(m: re.Match) -> str | None:
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y + 1911:04d}-{mo:02d}-{d:02d}"


def _norm(s: str) -> str:
    """PDF 常用康熙部首等相容字形（⽉⽇⼀…）排版，先做 NFKC 正規化成一般漢字。"""
    return unicodedata.normalize("NFKC", s or "")


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", "", _norm(s)).strip()


def _doc_numbers(text: str) -> list[str]:
    out = []
    for d in DOC_NO.findall(re.sub(r"\s+", "", text)):
        d = re.split(r"[日依]", d)[-1]          # 去掉黏在前面的日期
        d = re.split(r"文號[:：]?", d)[-1]      # 去掉「備查文號」等標籤
        d = re.sub(r"^[\d.]+", "", d)           # 去掉黏在前面的點分日期數字
        if d and d not in out:
            out.append(d)
    return out


def _cluster(values: Iterable[float], tol: float = 2.0) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _separators(tops: list[float], tol: float = 1.6, min_thickness: float = 0.3) -> list[float]:
    """商品之間的分隔線是「有厚度的線」（PDF 以細長矩形繪製，上下兩條邊相距 0.5–3pt）；
    同一商品內換行用的細線只有單一條邊（厚度 0）。只保留有厚度的線當商品邊界。"""
    groups: list[list[float]] = []
    for v in sorted(tops):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    thick = [g[0] for g in groups if g[-1] - g[0] >= min_thickness]
    return thick if len(thick) >= 2 else [g[0] for g in groups]


def parse_disclosure_pdf(data: bytes, name_prefix: str | None = None) -> list[ProductRow]:
    """name_prefix（如「富邦人壽」）：跨頁時下一頁開頭只剩名稱後半段，沒有公司名開頭者併回上一筆。"""
    rows: list[ProductRow] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages):
            for table in page.find_tables():
                for r in _parse_table(page, table, pno):
                    if name_prefix and rows and not r.name.startswith(name_prefix):
                        prev = rows[-1]
                        prev.name += r.name
                        prev.approvals += r.approvals
                        prev.doc_numbers += [d for d in r.doc_numbers if d not in prev.doc_numbers]
                        ds = [d for d in (prev.first_date, prev.latest_date, r.first_date, r.latest_date) if d]
                        prev.first_date, prev.latest_date = (min(ds), max(ds)) if ds else (None, None)
                        prev.is_investment = bool(INVESTMENT.search(prev.name))
                        prev.is_rider = bool(RIDER.search(prev.name))
                        continue
                    rows.append(r)
    return _merge_split(rows)


def _parse_table(page, table, pno: int) -> list[ProductRow]:
    x0, top, x1, bottom = table.bbox
    vxs = _cluster([e["x0"] for e in page.edges
                    if e["orientation"] == "v" and top - 1 <= e["top"] <= bottom
                    and x0 - 3 <= e["x0"] <= x1 + 3 and e["height"] >= 8])
    inner = [v for v in vxs if v > x0 + 20]
    if not inner:
        return []
    name_x1 = inner[0]
    seps = _separators([e["top"] for e in page.edges
                        if e["orientation"] == "h" and top - 1 <= e["top"] <= bottom + 1
                        and e["x0"] <= x0 + 8 and e["x1"] >= name_x1 - 8])
    out = []
    for a, b in zip(seps, seps[1:]):
        if b - a < 4:
            continue
        name = _clean_name(page.crop((x0, a, name_x1, b)).extract_text() or "")
        docs = _norm(page.crop((name_x1, a, x1, b)).extract_text() or "").strip()
        if not name or "商品名稱" in name or name in CATEGORY_WORDS or not docs:
            continue
        lines = [ln.strip() for ln in docs.splitlines() if ln.strip()]
        dates = [d for d in (_iso(m) for m in ROC_DATE.finditer(docs)) if d]
        dotted = re.findall(r"(?<!\d)(\d{2,3})\.(\d{2})\.(\d{2})(?!\d)", docs)  # 富邦／凱基：101.07.01
        dates += [f"{int(y)+1911:04d}-{m}-{d}" for y, m, d in dotted]
        out.append(ProductRow(
            name=name,
            approvals=lines,
            first_date=min(dates) if dates else None,
            latest_date=max(dates) if dates else None,
            doc_numbers=_doc_numbers(docs),
            is_investment=bool(INVESTMENT.search(name)),
            is_rider=bool(RIDER.search(name)),
            page=pno + 1,
        ))
    return out


def _merge_split(rows: list[ProductRow]) -> list[ProductRow]:
    """跨頁時同一商品可能被切成兩段（下一頁開頭的名稱不完整）；名稱完全相同者也合併。"""
    merged: dict[str, ProductRow] = {}
    for r in rows:
        if r.name in merged:
            m = merged[r.name]
            m.approvals += r.approvals
            m.doc_numbers += r.doc_numbers
            ds = [d for d in (m.first_date, m.latest_date, r.first_date, r.latest_date) if d]
            m.first_date, m.latest_date = (min(ds), max(ds)) if ds else (None, None)
        else:
            merged[r.name] = r
    return list(merged.values())
