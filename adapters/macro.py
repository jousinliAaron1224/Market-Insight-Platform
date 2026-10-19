"""總體經濟指標（A 級，2026-10-19 新增）：美債殖利率、聯準會利率、美元兌新台幣、台灣央行利率。

- FRED（美國聖路易聯邦準備銀行）公開 CSV：https://fred.stlouisfed.org/graph/fredgraph.csv?id=<序列>&cosd=<起日>
  robots.txt 只限制圖片與搜尋頁，Crawl-delay 1 秒；不需要 API 金鑰。
  DGS10 美國 10 年期公債殖利率、DGS2 2 年期、DFEDTARU 聯準會聯邦基金利率目標區間上限、DEXTAUS 美元兌新台幣（日資料）
- 中央銀行（政府資料開放平臺 10783）：EG28D01.csv，每次調整一列（重貼現率、擔保放款融通利率、短期融通利率）

每個序列一個 item。raw 存原始 CSV；解析後的數列放在 meta["points"]（[[日期, 數值], …]），匯出時直接讀。
CSV 內容每天都會多一列，所以每次抓取都會是新版本（量很小：只抓近幾年）。
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from adapters.common import content_fingerprint
from core.storage import utcnow


def parse_fred(text: str) -> list[list]:
    """FRED CSV：observation_date,<序列>；缺值是空白或「.」，略過。"""
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[1:]:
        if len(r) >= 2 and r[1] not in ("", "."):
            try:
                out.append([r[0], round(float(r[1]), 4)])
            except ValueError:
                continue
    return out


def parse_cbc(text: str) -> list[list]:
    """央行利率 CSV："日","重貼現(%)","擔保放款融通(%)","短期融通(%)"；日期 yyyymmdd → [日期, 重貼現, 擔保放款, 短期融通]。"""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for r in rows[1:]:
        if len(r) >= 4 and len(r[0]) == 8 and r[0].isdigit():
            try:
                out.append([f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:]}", float(r[1]), float(r[2]), float(r[3])])
            except ValueError:
                continue
    return out


class MacroAdapter(SourceAdapter):
    source_id = "macro"
    tier = "A"
    domain = "multiple"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.series: list[dict[str, Any]] = config.get("series", [])
        self.years = int(config.get("years", 5))
        self.respect_robots: bool = bool(config.get("respect_robots", True))

    def _url(self, s: dict[str, Any]) -> str:
        if s["kind"] == "fred":
            since = (date.today() - timedelta(days=365 * self.years)).isoformat()
            return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={s['code']}&cosd={since}"
        return s["url"]

    def list_items(self) -> list[ItemRef]:
        return [ItemRef(self.source_id, f"macro:{s['id']}", self._url(s), s["name"], None) for s in self.series]

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        s = next(x for x in self.series if f"macro:{x['id']}" == ref.item_key)
        resp = self.ctx.http.get(ref.url, respect_robots=self.respect_robots)
        if resp.status_code == 304:
            raise NotModified(ref.url)
        raw = resp.content
        text = raw.decode("utf-8-sig", errors="replace")
        points = parse_fred(text) if s["kind"] == "fred" else parse_cbc(text)
        if not points:
            raise ValueError(f"{s['id']}：解析不到任何數值（格式可能改了）")
        path, _ = self.ctx.raw.put(self.source_id, raw, "csv", now)
        meta = {"title": s["name"], "series": s["id"], "kind": s["kind"], "unit": s.get("unit"), "source_name": s.get("source"),
                "page": s.get("page"), "points": points, "latest": points[-1], "event_extra": {"series": s["id"]}}
        return RawDoc(self.source_id, ref.item_key, ref.url, now, content_fingerprint({"points": points}), "csv",
                      path, None, None, meta)
