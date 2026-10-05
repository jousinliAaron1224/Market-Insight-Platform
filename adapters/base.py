"""SourceAdapter 介面與共用資料結構（Handbook「爬蟲設計」）。

每個來源一個 adapter：list_items 只碰列表頁，fetch 抓單一項目並存 raw。
新舊判斷一律交給 core.change_detect，adapter 不碰資料庫。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 避免循環 import
    from core.context import AdapterContext


@dataclass
class ItemRef:
    source_id: str
    item_key: str  # guid 或正規化後的 URL
    url: str
    title: str | None
    published_at: datetime | None


@dataclass
class RawDoc:
    source_id: str
    item_key: str
    url: str
    fetched_at: datetime
    content_hash: str  # 變動偵測指紋：預設 sha256(原始位元組)；頁面有雜訊時可改用正文指紋（D11）
    doc_type: str  # html | pdf | csv | rss_item | list_row（只記列表列，未抓內文）
    raw_path: str  # 原始檔在 raw 儲存的位置（相對 data/raw 的上層）
    http_etag: str | None
    http_last_modified: str | None
    meta: dict[str, Any] = field(default_factory=dict)  # 來源特有欄位；meta['event_extra'] 會併入事件 payload


class NotModified(Exception):
    """HTTP 層回 304：內容沒變，不下載。由 change_detect 捕捉並計數。"""


class SourceAdapter(ABC):
    source_id: str
    tier: str  # A | B | C | D
    schedule: str  # cron 表達式（來自 config/sources.yaml）
    domain: str  # 限速用

    def __init__(self, ctx: "AdapterContext", config: dict[str, Any]):
        self.ctx = ctx
        self.config = config
        self.tier = config.get("tier", getattr(self, "tier", ""))
        self.schedule = config.get("schedule", getattr(self, "schedule", ""))

    @abstractmethod
    def list_items(self) -> list[ItemRef]:
        """只抓列表頁，絕不下載內文。"""

    @abstractmethod
    def fetch(self, ref: ItemRef) -> RawDoc:
        """抓單一項目並存 raw。必須冪等；HTTP 304 時 raise NotModified。"""
