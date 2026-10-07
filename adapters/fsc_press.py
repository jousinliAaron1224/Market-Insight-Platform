"""金管會新聞稿（B 級，列表頁解析）。

- 列表：https://www.fsc.gov.tw/ch/home.jsp?id=96 ，每頁 15 筆，`page=N` 翻頁；
  每列有發布日期、資料來源（發布單位）、標題、dataserno。
- 內文：news_view.jsp?dataserno=N ，含正文、聯絡單位與「相關附件」PDF 連結。

決策（2026-10-02，Handbook D12–D13）：
- 只抓「保險局、金融監督管理委員會（本部）、檢查局」的內文；其他單位只記列表列（doc_type=list_row）。
- 內文頁含每次都會變的「瀏覽人次」，content_hash 以正文＋附件清單計算（D11），raw 存原始位元組。
- 無 ETag / Last-Modified；robots.txt 只限制 Googlebot，照常遵守。
- 附件表格（2026-10-07）：附件名稱命中 config 的 attachment_tables 時，下載該 PDF、存 raw，
  並把表格存進 meta["tables"]（例如「外幣保單新契約保費收入占整體新契約保費收入比率」，市場數據的幣別結構用）。
  附件下載失敗不影響新聞稿本身，只記 meta["table_errors"]。
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.parser import HTMLParser

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from adapters.common import TPE, content_fingerprint, node_text
from core.storage import utcnow

BASE = "https://www.fsc.gov.tw/ch/"
DEFAULT_UNITS = ["保險局", "金融監督管理委員會", "檢查局"]


def canonical_news_url(dataserno: str) -> str:
    return (f"{BASE}home.jsp?id=96&parentpath=0&mcustomize=news_view.jsp"
            f"&dataserno={dataserno}&dtable=News")


def list_page_url(page: int) -> str:
    if page <= 1:
        return f"{BASE}home.jsp?id=96"
    return (f"{BASE}home.jsp?id=96&contentid=96&parentpath=0"
            f"&mcustomize=news_list.jsp&page={page}&pagesize=15")


def parse_list(html: bytes | str) -> list[dict[str, str]]:
    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    rows = []
    for li in tree.css(".newslist li[role=row]"):
        a = li.css_first(".title a")
        date = li.css_first(".date")
        unit = li.css_first(".unit")
        if a is None or date is None:  # 表頭列
            continue
        href = a.attributes.get("href") or ""
        serno = (parse_qs(urlsplit(href).query).get("dataserno") or [""])[0]
        if not serno:
            continue
        rows.append({
            "dataserno": serno,
            "date": date.text(strip=True),
            "unit": unit.text(strip=True) if unit else "",
            # 列表文字會截斷成「……」，完整標題在 title 屬性
            "title": (a.attributes.get("title") or a.text(strip=True)).strip(),
        })
    return rows


def pdf_tables(data: bytes) -> list[list[list[str]]]:
    """PDF 附件裡的表格（pdfplumber）；每格去空白，空列略過。"""
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables():
                rows = [[re.sub(r"\s+", "", c or "") for c in r] for r in t]
                rows = [r for r in rows if any(r)]
                if rows:
                    out.append(rows)
    return out


def parse_detail(html: bytes, base_url: str) -> dict[str, Any]:
    tree = HTMLParser(html.decode("utf-8", "replace"))
    mc = tree.css_first(".maincontent")
    meta: dict[str, Any] = {}
    warnings: list[str] = []
    if mc is None:
        return {"parse_warnings": ["missing .maincontent"], "body_text": "", "attachments": []}

    subj = mc.css_first(".subject")
    meta["detail_title"] = subj.text(strip=True) if subj else ""
    d = mc.css_first(".date")
    meta["announced_date"] = d.text(strip=True) if d else ""
    body = node_text(mc.css_first(".page-edit"))
    meta["body_text"] = body
    m = re.search(r"聯絡單位[:：]\s*(\S+)", body)
    if m:
        meta["contact_unit"] = m.group(1)
    meta["attachments"] = [
        {"name": a.text(strip=True), "url": urljoin(base_url, a.attributes.get("href") or "")}
        for a in tree.css(".acces a")  # 「相關附件」在 .maincontent 之外
        if a.attributes.get("href")
    ]
    for k, label in (("detail_title", "missing subject"), ("announced_date", "missing date"),
                     ("body_text", "missing body")):
        if not meta[k]:
            warnings.append(label)
    if warnings:
        meta["parse_warnings"] = warnings
    return meta


class FscPressAdapter(SourceAdapter):
    source_id = "fsc_press"
    tier = "B"
    domain = "www.fsc.gov.tw"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.list_pages: int = int(config.get("list_pages", 1))
        self.units: set[str] = set(config.get("detail_units", DEFAULT_UNITS))
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._rows: dict[str, dict[str, str]] = {}
        # 附件名稱的 regex → 下載並解析表格
        self.attachment_tables = [re.compile(x) for x in config.get("attachment_tables", [])]

    def _attachment_tables(self, attachments: list[dict[str, str]], now) -> tuple[list[dict], list[str]]:
        tables, errors = [], []
        for a in attachments:
            if not any(p.search(a["name"]) for p in self.attachment_tables):
                continue
            try:
                # 附件下載點要帶 Referer，否則回網頁而不是 PDF
                resp = self.ctx.http.get(a["url"], respect_robots=self.respect_robots, headers={"Referer": BASE + "home.jsp"})
                if not resp.content.startswith(b"%PDF"):
                    raise ValueError("附件不是 PDF")
                path, _ = self.ctx.raw.put(self.source_id, resp.content, "pdf", now)
                tables.append({"name": a["name"], "url": a["url"], "raw_path": path, "tables": pdf_tables(resp.content)})
            except Exception as e:   # 附件失敗不影響新聞稿本身
                errors.append(f"{a['name']}: {e!r}")
        return tables, errors

    def list_items(self) -> list[ItemRef]:
        self._rows.clear()
        refs: list[ItemRef] = []
        for page in range(1, self.list_pages + 1):
            resp = self.ctx.http.get(list_page_url(page), respect_robots=self.respect_robots)
            rows = parse_list(resp.content)
            if not rows and page == 1:
                raise ValueError("fsc_press: 列表頁解析出 0 筆，可能改版")
            refs.extend(self._to_ref(r) for r in rows if self._remember(r))
        return refs

    def _remember(self, row: dict[str, str]) -> bool:
        key = canonical_news_url(row["dataserno"])
        if key in self._rows:  # 翻頁時資料剛好位移，避免重複
            return False
        self._rows[key] = row
        return True

    def _to_ref(self, row: dict[str, str]) -> ItemRef:
        try:
            pub = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=TPE)
        except ValueError:
            pub = None
        url = canonical_news_url(row["dataserno"])
        return ItemRef(self.source_id, url, url, row["title"] or None, pub)

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        row = self._rows.get(ref.item_key) or {
            "dataserno": (parse_qs(urlsplit(ref.url).query).get("dataserno") or [""])[0],
            "date": ref.published_at.strftime("%Y-%m-%d") if ref.published_at else "",
            "unit": "", "title": ref.title or "",
        }
        in_scope = row["unit"] in self.units
        meta: dict[str, Any] = {
            "title": ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "unit": row["unit"],
            "in_scope": in_scope,
            "dataserno": row["dataserno"],
            "event_extra": {"unit": row["unit"], "in_scope": in_scope},
        }

        if not in_scope:  # 只記列表列，不抓內文
            data = json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            path, digest = self.ctx.raw.put(self.source_id, data, "json", now)
            return RawDoc(self.source_id, ref.item_key, ref.url, now, digest, "list_row",
                          path, None, None, meta)

        etag, last_mod = self.ctx.validators(self.source_id, ref.item_key)
        resp = self.ctx.http.get(ref.url, etag=etag, last_modified=last_mod,
                                 respect_robots=self.respect_robots)
        if resp.status_code == 304:
            raise NotModified(ref.url)
        path, raw_digest = self.ctx.raw.put(self.source_id, resp.content, "html", now)
        detail = parse_detail(resp.content, ref.url)
        body = detail.pop("body_text", "")
        meta.update(detail)
        if self.attachment_tables and detail.get("attachments"):
            tables, errors = self._attachment_tables(detail["attachments"], now)
            if tables:
                meta["tables"] = tables
            if errors:
                meta["table_errors"] = errors
        meta["body_chars"] = len(body)
        meta["raw_sha256"] = raw_digest
        fingerprint = content_fingerprint({
            "title": meta.get("detail_title"), "date": meta.get("announced_date"),
            "body": body, "attachments": meta.get("attachments"),
            **({"tables": [t["tables"] for t in meta["tables"]]} if meta.get("tables") else {}),
        })
        return RawDoc(
            source_id=self.source_id, item_key=ref.item_key, url=ref.url, fetched_at=now,
            content_hash=fingerprint, doc_type="html", raw_path=path,
            http_etag=resp.headers.get("ETag"), http_last_modified=resp.headers.get("Last-Modified"),
            meta=meta,
        )
