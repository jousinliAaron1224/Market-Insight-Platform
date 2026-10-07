"""有禮貌的 HTTP client：統一 User-Agent、每網域限速、robots.txt、conditional GET。"""
from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import httpx

from core.ratelimit import DomainRateLimiter


log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    pass


class TLSVerifyError(Exception):
    """TLS 憑證驗證失敗：設定問題，不是暫時性錯誤，所以不重試（D18）。"""


def _cert_verify_failure(e: BaseException) -> ssl.SSLCertVerificationError | None:
    """httpx 把 ssl 例外包在 ConnectError 裡；沿著 __cause__／__context__ 找。"""
    seen = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return cur
        cur = cur.__cause__ or cur.__context__
    if "CERTIFICATE_VERIFY_FAILED" in str(e):
        return ssl.SSLCertVerificationError(str(e))
    return None


def tls_hint(host: str, reason: str) -> str:
    """依失敗原因給出既有決策的修法（D7 補簽發者憑證／D15 關 X.509 格式嚴格檢查）。"""
    if "Subject Key Identifier" in reason or "Authority Key Identifier" in reason:
        fix = "憑證缺少 Key Identifier 欄位：在這個來源設定 x509_strict: false（D15，憑證鏈與主機名稱仍驗證）"
    elif "local issuer" in reason or "issuer certificate" in reason:
        fix = (f"伺服器沒送中繼憑證：執行 sh scripts/fetch_issuer_cert.sh {host}，"
               f"再把 config/certs/{host}.pem 加到這個來源的 extra_ca_files（D7）")
    else:
        fix = "請檢查憑證；不要關閉 TLS 驗證"
    return f"{host} TLS 憑證驗證失敗（{reason}）。{fix}"


@dataclass
class RetryPolicy:
    """網路錯誤依退避秒數重試（Handbook「可靠性與監控」）。

    只重試暫時性錯誤：連線／逾時等傳輸錯誤，以及 429、5xx。
    4xx（如 403 被擋、404）與解析錯誤不重試，直接記錄並警示。
    """
    max: int = 3
    backoff_sec: list[float] = field(default_factory=lambda: [30, 120, 600])

    @classmethod
    def from_config(cls, cfg: dict | None) -> "RetryPolicy":
        cfg = cfg or {}
        return cls(max=int(cfg.get("max", 3)), backoff_sec=list(cfg.get("backoff_sec", [30, 120, 600])))

    def delay(self, attempt: int) -> float:
        """attempt 從 1 起算；超出清單長度時沿用最後一個值。"""
        if not self.backoff_sec:
            return 0.0
        return float(self.backoff_sec[min(attempt, len(self.backoff_sec)) - 1])


def build_ssl_context(extra_ca_files: list[str | Path] | None = None,
                      x509_strict: bool = True) -> ssl.SSLContext:
    """certifi 的 Mozilla 根憑證，再加上來源特定的補充憑證。

    部分台灣網站只送出伺服器憑證、沒附中繼憑證（瀏覽器會自動補抓，Python 不會），
    或由不在 Mozilla 清單內的根憑證簽發。這時只「增加」信任的憑證，絕不關閉驗證。

    x509_strict=False（決策 D15）：Python 3.13 起預設開啟 VERIFY_X509_STRICT，
    會拒絕缺少 Subject Key Identifier 等欄位的憑證（如 www.fsc.gov.tw 的憑證鏈），
    瀏覽器與舊版 Python 都接受。只對設定的來源關掉這項「格式嚴格檢查」；
    憑證鏈、有效期與主機名稱驗證照常進行。
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    if not x509_strict:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    for f in extra_ca_files or []:
        path = Path(f)
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到補充憑證 {path}；請先執行 sh scripts/fetch_issuer_cert.sh {path.stem}（見 README）")
        ctx.load_verify_locations(cafile=str(path))
    return ctx


class PoliteClient:
    def __init__(self, user_agent: str, limiter: DomainRateLimiter,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
                 extra_ca_files: list[str | Path] | None = None,
                 x509_strict: bool = True,
                 retry: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.user_agent = user_agent
        self.limiter = limiter
        self.retry = retry or RetryPolicy(max=0)
        self._sleep = sleep
        self.retries_done = 0  # 供測試與 crawl 統計
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            verify=build_ssl_context(extra_ca_files, x509_strict),
        )
        self._robots: dict[str, RobotFileParser] = {}

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, *, etag: str | None = None, last_modified: str | None = None,
            respect_robots: bool = True, headers: dict[str, str] | None = None) -> httpx.Response:
        """HTTP 層變動偵測：帶 If-None-Match / If-Modified-Since；呼叫端自行判斷 304。

        headers：少數網站的 API 要求特定標頭（例如南山的 GET 也要 Content-Type: application/json，否則回 406）。
        """
        headers = dict(headers or {})
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return self._request("GET", url, headers, None, None, respect_robots)

    def post(self, url: str, *, data: dict[str, str] | None = None, json: Any = None,
             respect_robots: bool = True, headers: dict[str, str] | None = None) -> httpx.Response:
        """網站自己的查詢 API（例如兆豐的保險商品列表）只接受 POST（表單 data 或 JSON）；限速、robots、重試與 GET 相同。"""
        return self._request("POST", url, dict(headers or {}), data, json, respect_robots)

    def _request(self, method: str, url: str, headers: dict[str, str], data: dict[str, str] | None,
                 json: Any, respect_robots: bool) -> httpx.Response:
        if respect_robots and not self.allowed(url):
            raise RobotsDisallowed(url)
        domain = urlsplit(url).netloc
        attempt = 0
        while True:
            try:
                with self.limiter.slot(domain):
                    resp = self._client.request(method, url, headers=headers, data=data, json=json)
                if resp.status_code in RETRY_STATUS and attempt < self.retry.max:
                    raise _Retryable(f"HTTP {resp.status_code}")
                if resp.status_code != 304:
                    resp.raise_for_status()
                return resp
            except (httpx.TransportError, _Retryable) as e:
                bad = _cert_verify_failure(e)
                if bad is not None:          # 設定問題，重試也一樣失敗
                    reason = getattr(bad, "verify_message", None) or str(bad)
                    raise TLSVerifyError(tls_hint(urlsplit(url).hostname or domain, reason)) from e
                if attempt >= self.retry.max:
                    raise
                attempt += 1
                wait = self.retry.delay(attempt)
                log.warning("retry %d/%d in %.0fs for %s: %s", attempt, self.retry.max, wait, url, e)
                self.retries_done += 1
                self._sleep(wait)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = RobotFileParser()
            try:
                with self.limiter.slot(parts.netloc):
                    r = self._client.get(base + "/robots.txt")
                if r.status_code >= 400:
                    rp.parse([])  # 沒有 robots.txt 視為允許
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp.parse([])
            self._robots[base] = rp
        return rp.can_fetch(self.user_agent, url)


class _Retryable(Exception):
    """可重試的 HTTP 狀態（429／5xx），僅在 PoliteClient.get 內部使用。"""
