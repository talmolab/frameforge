"""Transfer worker (shared).

STUB: real scan scratch → copy finalized *.mp4 to paths.vast_dest → delete,
with retry/backoff and a low-disk pause, comes in the Transfer round.
"""
from __future__ import annotations

import logging
import time

from . import metrics

log = logging.getLogger("frameforge.transfer")


def run(cfg, drain, registry) -> None:
    log.info("[stub] transfer starting (dest=%s)", cfg.paths.vast_dest)
    n = 0
    while not drain.is_set():
        time.sleep(2.0)
        n += 1
        metrics.gauge(registry, "transfer.heartbeat", n)
    log.info("[stub] transfer flushing + stopping (n=%d)", n)
