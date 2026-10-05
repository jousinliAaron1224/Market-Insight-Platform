"""Adapter 執行時可用的共用資源（類似 firmware HAL 往下注入的 driver handle）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.http import PoliteClient
from core.storage import RawStore

# (source_id, item_key) -> (etag, last_modified)：最新版本的 HTTP validator，供 conditional GET
ValidatorLookup = Callable[[str, str], tuple[str | None, str | None]]


@dataclass
class AdapterContext:
    http: PoliteClient
    raw: RawStore
    validators: ValidatorLookup
