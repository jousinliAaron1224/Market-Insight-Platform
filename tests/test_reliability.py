"""重試退避與並發上限。"""
import threading
import time

import httpx
import pytest

from core.http import PoliteClient, RetryPolicy
from core.ratelimit import DomainRateLimiter


def client(handler, retry=RetryPolicy(max=3, backoff_sec=[30, 120, 600]), limiter=None):
    sleeps = []
    c = PoliteClient("TestBot/0.1", limiter or DomainRateLimiter(0),
                     transport=httpx.MockTransport(handler), retry=retry, sleep=sleeps.append)
    return c, sleeps


def test_retries_5xx_with_backoff_then_succeeds():
    calls = []
    def h(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=b"ok")
    c, sleeps = client(h)
    assert c.get("https://a.example/x", respect_robots=False).content == b"ok"
    assert sleeps == [30, 120] and len(calls) == 3


def test_network_error_retried_then_raises_after_max():
    def h(req):
        raise httpx.ConnectError("boom", request=req)
    c, sleeps = client(h)
    with pytest.raises(httpx.ConnectError):
        c.get("https://a.example/x", respect_robots=False)
    assert sleeps == [30, 120, 600]  # 3 次重試，共 4 次嘗試


def test_4xx_not_retried():
    calls = []
    def h(req):
        calls.append(1)
        return httpx.Response(403)
    c, sleeps = client(h)
    with pytest.raises(httpx.HTTPStatusError):
        c.get("https://a.example/x", respect_robots=False)
    assert sleeps == [] and len(calls) == 1


def test_5xx_exhausted_raises_status_error():
    c, sleeps = client(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        c.get("https://a.example/x", respect_robots=False)
    assert len(sleeps) == 3


def test_retry_policy_from_config():
    p = RetryPolicy.from_config({"max": 2, "backoff_sec": [5]})
    assert (p.max, p.delay(1), p.delay(2)) == (2, 5.0, 5.0)


def test_per_domain_concurrency_shared_across_clients():
    """兩個來源（兩個 client）打同一網域，共用 limiter 時同時只會有 1 個請求。"""
    limiter = DomainRateLimiter(0, per_domain_concurrency=1)
    active, peak, lock = [0], [0], threading.Lock()
    def h(req):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return httpx.Response(200)
    c1, _ = client(h, limiter=limiter)
    c2, _ = client(h, limiter=limiter)
    ts = [threading.Thread(target=c.get, args=(f"https://same.example/{i}",), kwargs={"respect_robots": False})
          for i, c in enumerate([c1, c2, c1, c2])]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1


def test_different_domains_run_in_parallel():
    limiter = DomainRateLimiter(0, per_domain_concurrency=1)
    active, peak, lock = [0], [0], threading.Lock()
    def h(req):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return httpx.Response(200)
    c, _ = client(h, limiter=limiter)
    ts = [threading.Thread(target=c.get, args=(f"https://d{i}.example/",), kwargs={"respect_robots": False})
          for i in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] >= 2


def test_min_interval_enforced():
    t = [0.0]
    slept = []
    lim = DomainRateLimiter(3, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    with lim.slot("x"):
        pass
    t[0] += 1
    with lim.slot("x"):
        pass
    assert slept == [2.0]
