"""總體經濟指標（A 級，2026-10-19 新增）：美債殖利率、聯準會利率、美元兌新台幣、台灣央行利率。

- FRED（美國聖路易聯邦準備銀行）公開 CSV：https://fred.stlouisfed.org/graph/fredgraph.csv?id=<序列>&cosd=<起日>
  robots.txt 只限制圖片與搜尋頁，Crawl-delay 1 秒；不需要 API 金鑰。
  DGS10 美國 10 年期公債殖利率、DGS2 2 年期、DFEDTARU 聯準會聯邦基金利率目標區間上限、DEXTAUS 美元兌新台幣（日資料）
- 中央銀行（政府資料開放平臺 10783）：EG28D01.csv，每次調整一列（重貼現率、擔保放款融通利率、短期融通利率）

- 中央銀行資本市場利率（政府資料開放平臺 10840）：EG43M01.csv，十年期政府公債次級市場利率（月）
- 主計總處消費者物價基本分類指數（6019，XML）：總指數、醫藥保健類、醫療費用、藥品及保健食品的年增率（月）
- 主計總處受僱員工每人每月經常性薪資（9663，XML）：工業及服務業、金融及保險業（月）
  這兩份的下載網址用平臺 API（/api/v2/rest/dataset/<編號>）查，主計總處換檔名也不會壞。

每個設定項目一個 item。raw 存原始檔；解析後的數列放在 meta["points"]（[[日期, 數值], …]），匯出時直接讀。
一個檔案拆成多個序列的（CPI、薪資）另外放在 meta["multi"]（{序列id: {name, unit, points}}）。
CSV 內容每天都會多一列，所以每次抓取都會是新版本（量很小：只抓近幾年）。
"""
from __future__ import annotations

import csv
import io
import json
import re
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


def parse_cbc_bond(text: str, column: str = "十年期政府公債次級市場利率") -> list[list]:
    """央行資本市場利率月資料：「2026M08」→ [2026-08-01, 數值]；「-」略過。"""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    col = next(i for i, h in enumerate(rows[0]) if h.startswith(column))
    out = []
    for r in rows[1:]:
        m = re.fullmatch(r"(\d{4})M(\d{2})", r[0].strip()) if r else None
        if m and len(r) > col:
            try:
                out.append([f"{m.group(1)}-{m.group(2)}-01", float(r[col])])
            except ValueError:
                continue
    return out


OBS = re.compile(r"<Obs><Item>([^<]*)</Item><TIME_PERIOD>(\d{4})M(\d{2})</TIME_PERIOD><FREQ>M</FREQ><TYPE>([^<]*)</TYPE>\s*<Item_VALUE>([^<]*)</Item_VALUE></Obs>")


def parse_dgbas_cpi(text: str, items: dict[str, str], kind: str = "年增率(%)") -> dict[str, list[list]]:
    """主計總處 CPI 基本分類 XML → {序列id: [[月初日期, 年增率]]}。items：{序列id: 項目名稱開頭}（例 "五.醫藥保健類"）。"""
    out: dict[str, list[list]] = {k: [] for k in items}
    for name, y, mo, typ, val in OBS.findall(text):
        if typ != kind or not val.strip():
            continue
        for sid, prefix in items.items():
            if name.startswith(prefix):
                out[sid].append([f"{y}-{mo}-01", float(val)])
    return {k: sorted(v) for k, v in out.items()}


def parse_dgbas_wage(text: str, cols: dict[str, str]) -> dict[str, list[list]]:
    """主計總處每人每月經常性薪資 XML → {序列id: [[月初日期, 元]]}。只取月資料（年月 6 位數，Ⓟ初步值也收）；cols：{序列id: 欄位名稱開頭}。"""
    out: dict[str, list[list]] = {k: [] for k in cols}
    for block in re.findall(r"<每人每月經常性薪資>(.*?)</每人每月經常性薪資>", text, re.S):
        ym = re.search(r"<年月別_Year_and_month>(\d{6})\D*</年月別_Year_and_month>", block)
        if not ym:
            continue
        d = f"{ym.group(1)[:4]}-{ym.group(1)[4:]}-01"
        for sid, prefix in cols.items():
            m = re.search(rf"<{re.escape(prefix)}[^>]*>([\d.]+)</", block)
            if m:
                out[sid].append([d, float(m.group(1))])
    return {k: sorted(v) for k, v in out.items()}


class MacroAdapter(SourceAdapter):
    source_id = "macro"
    tier = "A"
    domain = "multiple"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.series: list[dict[str, Any]] = config.get("series", [])
        self.years = int(config.get("years", 5))
        self.history_from = config.get("history_from")   # 長期月資料起點（例 1990-01-01）；網站圖表可以縮放看更長的期間
        self.respect_robots: bool = bool(config.get("respect_robots", True))

    def _url(self, s: dict[str, Any]) -> str:
        if s["kind"] == "fred":
            since = (date.today() - timedelta(days=365 * self.years)).isoformat()
            return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={s['code']}&cosd={since}"
        if s.get("dataset"):   # 政府資料開放平臺：用 API 查目前的下載網址（match 指定要哪個檔）
            resp = self.ctx.http.get(f"https://data.gov.tw/api/v2/rest/dataset/{s['dataset']}", respect_robots=self.respect_robots)
            files = [f.get("resourceDownloadUrl") for f in json.loads(resp.content.decode("utf-8-sig"))["result"]["distribution"]]
            return next(u for u in files if u and s.get("match", "") in u)
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
        multi = None
        if s["kind"] == "dgbas_cpi":
            multi = parse_dgbas_cpi(text, {o["id"]: o["item"] for o in s["outputs"]})
        elif s["kind"] == "dgbas_wage":
            multi = parse_dgbas_wage(text, {o["id"]: o["col"] for o in s["outputs"]})
        if multi is not None:
            if not all(multi.values()):
                raise ValueError(f"{s['id']}：有序列解析不到數值（格式可能改了）：{[k for k, v in multi.items() if not v]}")
            points = multi[s["outputs"][0]["id"]]
        else:
            points = {"fred": parse_fred, "cbc": parse_cbc, "cbc_bond": parse_cbc_bond}[s["kind"]](text)
        if not points:
            raise ValueError(f"{s['id']}：解析不到任何數值（格式可能改了）")
        history = None
        if s["kind"] == "fred" and self.history_from:   # 另抓一份長期月底值（FRED 端彙總，不必下載幾十年的日資料）
            h = self.ctx.http.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={s['code']}&cosd={self.history_from}&fq=Monthly&fam=eop",
                                  respect_robots=self.respect_robots)
            history = parse_fred(h.content.decode("utf-8-sig", errors="replace"))
        ext = "xml" if s["kind"].startswith("dgbas") else "csv"
        path, _ = self.ctx.raw.put(self.source_id, raw, ext, now)
        meta = {"title": s["name"], "series": s["id"], "kind": s["kind"], "unit": s.get("unit"), "source_name": s.get("source"),
                "page": s.get("page"), "group": s.get("group"), "transform": s.get("transform"), "points": points, "latest": points[-1],
                "event_extra": {"series": s["id"]}}
        if history:
            meta["history"] = history
        if multi is not None:
            meta["multi"] = {o["id"]: {"name": o["name"], "unit": o.get("unit", s.get("unit")), "transform": o.get("transform"), "points": multi[o["id"]]} for o in s["outputs"]}
        return RawDoc(self.source_id, ref.item_key, ref.url, now, content_fingerprint({"points": points, "multi": meta.get("multi"), "history": history}), ext,
                      path, None, None, meta)
