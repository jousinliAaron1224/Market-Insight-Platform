"""共用測試工具：用 httpx.MockTransport 模擬網站，不打真實網路。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

from core.context import AdapterContext  # noqa: E402
from core.http import PoliteClient  # noqa: E402
from core.ratelimit import DomainRateLimiter  # noqa: E402
from core.storage import Database, RawStore  # noqa: E402


class FakeSite:
    """以 dict 定義 URL -> (status, body, headers)；記錄每個請求，可中途改內容。"""

    def __init__(self, routes: dict[str, tuple[int, bytes, dict[str, str]]]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.routes:
            return httpx.Response(404)
        status, body, headers = self.routes[url]
        etag = headers.get("ETag")
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(status, content=body, headers=headers)

    def hits(self, needle: str) -> int:
        return sum(needle in str(r.url) for r in self.requests)


@pytest.fixture
def make_env(tmp_path) -> Callable[..., tuple]:
    def _make(site: FakeSite):
        db = Database(tmp_path / "intel.db")
        db.init_schema()
        raw = RawStore(tmp_path / "data" / "raw")
        http = PoliteClient("TestBot/0.1", DomainRateLimiter(0),
                            transport=httpx.MockTransport(site.handler))
        return db, raw, http
    return _make


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def make_ctx(raw: RawStore, http: PoliteClient, validators=None) -> AdapterContext:
    return AdapterContext(http=http, raw=raw,
                          validators=validators or (lambda s, k: (None, None)))
