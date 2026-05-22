"""Event-bus seam (aux input streams).

STUB: real timestamped event seam (in-process queue → sidecar timeseries,
aligned to video via host-monotonic ts) comes in the Event-Bus round. No MVP
integrations; never couples into the acq/encode hot loops.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("frameforge.eventbus")


def run(cfg, drain, registry) -> None:
    log.info("[stub] event-bus seam starting (no MVP sink)")
    while not drain.is_set():
        time.sleep(5.0)
    log.info("[stub] event-bus seam stopping")
