"""Process-wide logging: stdout only (systemd journal captures it).

Canonical line format: ``YYYY-MM-DD HH:MM:SS LEVEL [worker] message``.
The ``[worker]`` tag comes from ``multiprocessing.Process.name`` (set by the
supervisor when spawning each worker) via ``%(processName)s``.

Dedup is automatic: identical lines (same logger + level + fully-formatted
message) collapse within a time window, first occurrence always fires, and the
key re-fires once the interval elapses — no call-site changes needed. Call
sites can still attach ``extra={DEDUP_KEY: <key>, DEDUP_INTERVAL_S: <seconds>}``
to group differently-worded lines under one key or override the window.
Records carrying a traceback (``logger.exception``) are never auto-deduped, so
distinct failures always surface.
"""

import logging
import sys
import time

_FORMAT = "%(asctime)s %(levelname)-5s [%(processName)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_DEDUP_DEFAULT_INTERVAL_S = 30.0
_DEDUP_MAX_KEYS = 2048
_DEDUP_EVICT_TTL_S = 300.0

DEDUP_KEY = "dedup_key"
DEDUP_INTERVAL_S = "dedup_interval_s"


class _DedupFilter(logging.Filter):
    def __init__(self, default_interval_s: float) -> None:
        super().__init__()
        self._default_interval_s = default_interval_s
        self._last_emit: dict = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = getattr(record, DEDUP_KEY, None)
        if key is None:
            if record.exc_info:
                return True
            key = (record.name, record.levelno, record.getMessage())
        interval_s = getattr(record, DEDUP_INTERVAL_S, self._default_interval_s)
        now = time.monotonic()
        last = self._last_emit.get(key)
        if last is not None and now - last < interval_s:
            return False
        self._last_emit[key] = now
        if len(self._last_emit) > _DEDUP_MAX_KEYS:
            self._evict(now)
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - _DEDUP_EVICT_TTL_S
        for key in [k for k, seen in self._last_emit.items() if seen < cutoff]:
            del self._last_emit[key]


def setup_logging(level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    root_logger.setLevel(level)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    for noisy in ("smbprotocol", "smbclient", "spnego"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_DedupFilter(_DEDUP_DEFAULT_INTERVAL_S))
    root_logger.addHandler(stream_handler)
