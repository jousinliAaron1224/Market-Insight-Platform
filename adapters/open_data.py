"""政府資料開放平臺（data.gov.tw）的統計資料集（A 級，D28；市場數據的需求面與市占率）。

設定（config/sources.yaml 的 open_data.datasets）列出資料集編號，例如：
- 14539   壽險業績統計（壽險公會）：新契約保費依險種，含投資型 → 需求面
- 104113  人身保險業保費收入統計表（月報）（保險局）：依公司別 → 市占率

做法：
1. list_items：呼叫平臺 API（/api/v2/rest/dataset/<編號>）取得資料集的檔案清單（distribution），
   每個檔案一個 ItemRef；published_at 用資料集的更新時間。只碰 API，不下載檔案。
2. fetch：下載檔案原封不動存 raw（CSV／XLS／XLSX／ODS／JSON…），內容變了就發 doc_revised（D3）。
3. 解析成時間序列是下一步：要先在 Mac 實際下載、看過欄位才寫（這個環境連不到 data.gov.tw）。

API 回應格式若和預期不同，錯誤訊息會列出實際的欄位名稱，方便調整。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from adapters.base import ItemRef, NotModified, RawDoc, SourceAdapter
from adapters.common import TPE
from core.storage import utcnow

API = "https://data.gov.tw/api/v2/rest/dataset/{id}"
EXT = {"csv": "csv", "xls": "xls", "xlsx": "xlsx", "ods": "ods", "json": "json", "xml": "xml", "zip": "zip",
       "pdf": "pdf", "txt": "txt"}


def _pick(d: dict[str, Any], *names: str) -> Any:
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def parse_dataset(data: bytes, dataset_id: str) -> dict[str, Any]:
    """平臺 API 回應 → {title, modified, files:[{id, url, format, description}]}"""
    doc = json.loads(data.decode("utf-8-sig"))
    result = doc.get("result", doc) if isinstance(doc, dict) else {}
    if not isinstance(result, dict):
        raise ValueError(f"資料集 {dataset_id}：API 回應不是預期的格式（{type(result).__name__}）")
    dist = _pick(result, "distribution", "distributions", "resources")
    if not isinstance(dist, list):
        raise ValueError(f"資料集 {dataset_id}：API 回應沒有檔案清單（distribution）；實際欄位：{sorted(result)[:30]}")
    files = []
    for i, f in enumerate(dist):
        url = _pick(f, "resourceDownloadUrl", "downloadURL", "accessURL", "url")
        if not url:
            continue
        fmt = str(_pick(f, "resourceFormat", "format", "mediaType") or "").lower()
        files.append({
            "id": str(_pick(f, "resourceID", "resourceId", "id") or i),
            "url": url.strip(),
            "format": fmt,
            "description": _pick(f, "resourceDescription", "description", "title") or "",
        })
    if not files:
        raise ValueError(f"資料集 {dataset_id}：檔案清單是空的；第一筆欄位：{sorted(dist[0])[:30] if dist else []}")
    return {"title": _pick(result, "title", "datasetName") or dataset_id,
            "modified": _pick(result, "modifiedDate", "modified", "updateDate", "metadata_modified"),
            "files": files}


def _parse_date(s: Any) -> datetime | None:
    if not s:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TPE)


def ext_for(fmt: str, url: str, content_type: str | None) -> str:
    for key in (fmt, url.rsplit(".", 1)[-1].split("?")[0].lower(), (content_type or "").split("/")[-1]):
        k = (key or "").lower()
        for name, ext in EXT.items():
            if name == k or k.endswith(name):
                return ext
    return "bin"


class OpenDataAdapter(SourceAdapter):
    source_id = "open_data"
    tier = "A"
    domain = "data.gov.tw"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.datasets: list[dict[str, Any]] = config.get("datasets", [])
        self.respect_robots: bool = bool(config.get("respect_robots", True))
        self._files: dict[str, dict[str, Any]] = {}
        self.list_errors: list[str] = []

    def list_items(self) -> list[ItemRef]:
        self._files.clear()
        self.list_errors = []
        refs: list[ItemRef] = []
        for ds in self.datasets:
            if not ds.get("enabled", True):
                continue
            try:
                resp = self.ctx.http.get(API.format(id=ds["id"]), respect_robots=self.respect_robots)
                info = parse_dataset(resp.content, str(ds["id"]))
            except Exception as e:                 # 單一資料集失敗不影響其他
                self.list_errors.append(f"{ds.get('name', ds['id'])}: {e!r}")
                continue
            pub = _parse_date(info["modified"])
            for f in info["files"]:
                key = f"{ds['name']}:{f['id']}"
                self._files[key] = {**f, "dataset_id": str(ds["id"]), "dataset": ds["name"],
                                    "dataset_title": info["title"], "purpose": ds.get("purpose")}
                refs.append(ItemRef(self.source_id, key, f["url"],
                                    f"{info['title']}｜{f['description'] or f['format']}", pub))
        if self.datasets and not refs and self.list_errors:
            raise RuntimeError("; ".join(self.list_errors))
        return refs

    def fetch(self, ref: ItemRef) -> RawDoc:
        now = utcnow()
        f = self._files.get(ref.item_key)
        if f is None:
            raise KeyError(f"{ref.item_key} 不在最近一次 list_items 結果中")
        etag, last_mod = self.ctx.validators(self.source_id, ref.item_key)
        resp = self.ctx.http.get(f["url"], etag=etag, last_modified=last_mod, respect_robots=self.respect_robots)
        if resp.status_code == 304:
            raise NotModified(f["url"])
        ext = ext_for(f["format"], str(resp.url), resp.headers.get("Content-Type"))
        path, digest = self.ctx.raw.put(self.source_id, resp.content, ext, now)
        meta = {
            "title": ref.title,
            "published_at": ref.published_at.isoformat() if ref.published_at else None,
            "dataset_id": f["dataset_id"], "dataset": f["dataset"], "dataset_title": f["dataset_title"],
            "purpose": f.get("purpose"), "format": f["format"], "ext": ext, "description": f["description"],
            "final_url": str(resp.url), "bytes": len(resp.content),
            "event_extra": {"dataset": f["dataset"], "purpose": f.get("purpose")},
        }
        return RawDoc(self.source_id, ref.item_key, f["url"], now, digest, ext, path,
                      resp.headers.get("ETag"), resp.headers.get("Last-Modified"), meta)
