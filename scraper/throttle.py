"""Thread-safe primitives that govern one box's single outbound IP.

A box runs one rq worker process that fetches a batch of items concurrently.
`TokenBucket` caps the box's request rate so a single IP stays under eBay's
per-IP reputation threshold. `BoxProxyState` watches the recent challenge rate
and flips the whole box onto the residential proxy when the box IP starts
getting blocked, then probes the direct path again after a cooldown.
"""

import threading
import time
from collections import deque


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._rate = rate_per_sec
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            time.sleep(wait)


class BoxProxyState:
    """Tracks a rolling window of recent fetches and decides direct vs residential."""

    def __init__(self, threshold: float, cooldown_seconds: float, window: int = 50):
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._window = window
        self._recent: deque[bool] = deque(maxlen=window)
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def record(self, challenged: bool) -> None:
        with self._lock:
            self._recent.append(challenged)
            if challenged and self._challenge_rate() >= self._threshold:
                self._cooldown_until = time.monotonic() + self._cooldown

    def should_use_residential(self) -> bool:
        with self._lock:
            return time.monotonic() < self._cooldown_until

    def _challenge_rate(self) -> float:
        if not self._recent:
            return 0.0
        return sum(1 for c in self._recent if c) / len(self._recent)
