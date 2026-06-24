"""Process-wide logging: stdout only (systemd journal captures it).

Canonical line format: ``YYYY-MM-DD HH:MM:SS LEVEL [worker] message``.
The ``[worker]`` tag comes from ``multiprocessing.Process.name`` (set by the
supervisor when spawning each worker) via ``%(processName)s``.

Call sites that want dedup attach ``extra={DEDUP_KEY: <key>, DEDUP_INTERVAL_S:
<seconds>}`` to the log call. Same key within window is dropped; first call
always fires; interval defaults to ``_DEDUP_DEFAULT_INTERVAL_S`` when not set.
"""

import logging
import sys
import time

_FORMAT = "%(asctime)s %(levelname)-5s [%(processName)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_DEDUP_DEFAULT_INTERVAL_S = 30.0

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
            return True
        interval_s = getattr(record, DEDUP_INTERVAL_S, self._default_interval_s)
        now = time.monotonic()
        last = self._last_emit.get(key)
        if last is not None and now - last < interval_s:
            return False
        self._last_emit[key] = now
        return True


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
