"""解析層共用介面（Handbook 五層架構的第三層；M4，決策 D22）。

事件 → handler → 處理層資料表。handler 只讀 raw 與 events，不碰網路；
結果全部可由 raw 重建（刪掉解析表再跑一次即可）。

LLM 介面（D22：M4 先純規則）：Enricher 是規則結果之後的「補強」掛點，
預設 NullEnricher 不做任何事；之後接雲端 API 或本機模型時只要實作這個類別、
在 sources.yaml 的 parsing.llm 指定即可，handler 與資料表不用改。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.storage import Database, RawStore


class Enricher:
    """LLM 補強介面。規則抽不到的欄位（missing）與分類結果都會先交給它看一次。"""

    name = "none"

    def enrich_clause(self, fields: dict[str, Any], missing: list[str],
                      articles: list[dict[str, Any]]) -> dict[str, Any]:
        """回傳要補上的欄位（只能補 missing 內的欄位），每個欄位須附出處條號。"""
        return {}

    def refine_labels(self, text: str, labels: dict[str, Any]) -> dict[str, Any]:
        """可調整分類結果；預設原樣回傳。"""
        return labels


NullEnricher = Enricher


def load_enricher(spec: str | None) -> Enricher:
    """'none' 或 'package.module:ClassName'"""
    if not spec or spec == "none":
        return NullEnricher()
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)()


@dataclass
class ParseContext:
    db: Database
    raw: RawStore
    config: dict[str, Any] = field(default_factory=dict)   # sources.yaml 的 parsing 區塊
    enricher: Enricher = field(default_factory=NullEnricher)


class Handler(Protocol):
    name: str

    def handles(self, event: dict[str, Any]) -> bool: ...

    def handle(self, ctx: ParseContext, event: dict[str, Any]) -> dict[str, Any]:
        """處理單一事件，回傳一行摘要用的 dict（重播與 CLI 顯示用）。例外 = 本次失敗、之後重試。"""
        ...
