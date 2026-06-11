"""Log rate-limiting + sampled metric write helpers shared across workers."""

import time


RECURRING_WARN_INTERVAL_S = 30.0


# Use when the same warning can fire 50+/sec under stress and you want one
# consolidated line per interval instead of a flood.
class BurstAggregator:
    def __init__(self, interval_s: float = RECURRING_WARN_INTERVAL_S,
                 *, immediate: bool = False) -> None:
        self.interval_s = interval_s
        self._count = 0
        self._latest = None
        self._last_emit = 0.0 if immediate else time.monotonic()

    def event(self, latest=None):
        self._count += 1
        if latest is not None:
            self._latest = latest

        now = time.monotonic()
        if self._last_emit > 0 and now - self._last_emit < self.interval_s:
            return None

        elapsed = int(now - self._last_emit) if self._last_emit > 0 else 0
        snapshot = (self._count, elapsed, self._latest)

        self._count = 0
        self._latest = None
        self._last_emit = now
        return snapshot

    def reset(self) -> None:
        self._last_emit = 0.0
        self._count = 0
        self._latest = None


# Fires on the first call, then at most once per interval. Use for recurring
# conditions where the first breach should log immediately.
class RateLimited(BurstAggregator):
    def __init__(self, interval_s: float) -> None:
        super().__init__(interval_s, immediate=True)

    def should_log(self) -> bool:
        return self.event() is not None


# Cheap stand-in for histograms on hot paths: window max + last value
# pushed to gauges only every N observations.
class WindowMaxSampler:
    def __init__(self, every: int, gauge_last, gauge_max) -> None:
        self.every = every
        self.gauge_last = gauge_last
        self.gauge_max = gauge_max
        self._count = 0
        self._window_max = 0.0

    def observe(self, value: float) -> None:
        self._count += 1
        if value > self._window_max:
            self._window_max = value

        if self._count % self.every == 0:
            self.gauge_last.set(round(value, 4))
            self.gauge_max.set(round(self._window_max, 4))
            self._window_max = 0.0
