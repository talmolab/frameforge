"""Logging helpers used across workers.

Two patterns kept here so callers don't reinvent them:

- ``BurstAggregator`` counts many events and returns a summary tuple at the
  interval boundary. Use when the same warning can fire at high rate (e.g.
  50+/sec under stress) and you want one consolidated line per interval.
- ``RateLimited`` returns True from ``should_log()`` at most once per
  interval. Use when the message is already shaped right but the condition
  recurs every poll (LOW DISK, SMB reconnect failed). Call ``reset()`` when
  the condition clears so the next breach fires immediately.
"""

import time


RECURRING_WARN_INTERVAL_S = 30.0


class BurstAggregator:
    def __init__(self, interval_s: float = RECURRING_WARN_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._count = 0
        self._latest = None
        self._last_emit = time.monotonic()

    def event(self, latest=None):
        self._count += 1
        if latest is not None:
            self._latest = latest
        now = time.monotonic()
        if now - self._last_emit < self.interval_s:
            return None
        snapshot = (self._count, int(now - self._last_emit), self._latest)
        self._count = 0
        self._last_emit = now
        return snapshot


class RateLimited:
    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._last_emit = 0.0

    def should_log(self) -> bool:
        now = time.monotonic()
        if self._last_emit == 0.0 or now - self._last_emit >= self.interval_s:
            self._last_emit = now
            return True
        return False

    def reset(self) -> None:
        self._last_emit = 0.0
