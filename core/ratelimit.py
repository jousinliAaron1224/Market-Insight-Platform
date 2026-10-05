"""每網域限速與並發上限（Handbook「排程、限速與事件觸發」）。

- 同網域同時進行的請求數 ≤ per_domain_concurrency（預設 1）。
- 同網域兩次請求的開始時間間隔 ≥ min_interval_sec。
- 排程器同時跑多個來源時（例如 fsc_press 與 fsc_penalty 都打 www.fsc.gov.tw），
  所有來源共用同一個 DomainRateLimiter，限制才會跨來源生效。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable, Iterator


class DomainRateLimiter:
    def __init__(self, min_interval_sec: float, per_domain_concurrency: int = 1,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = float(min_interval_sec)
        self.concurrency = max(1, int(per_domain_concurrency))
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._interval_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._sem_guard = threading.Lock()

    def _sem(self, domain: str) -> threading.BoundedSemaphore:
        with self._sem_guard:
            if domain not in self._sems:
                self._sems[domain] = threading.BoundedSemaphore(self.concurrency)
            return self._sems[domain]

    def wait(self, domain: str) -> None:
        """只做間隔控制（不佔並發名額）。"""
        with self._interval_locks[domain]:
            last = self._last.get(domain)
            if last is not None:
                remaining = self.min_interval - (self._clock() - last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last[domain] = self._clock()

    @contextmanager
    def slot(self, domain: str) -> Iterator[None]:
        """佔一個並發名額並遵守間隔；請求在 with 區塊內進行。"""
        sem = self._sem(domain)
        sem.acquire()
        try:
            self.wait(domain)
            yield
        finally:
            sem.release()
