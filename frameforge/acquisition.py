"""Acquisition worker (one per camera).

STUB: real pypylon grab → .pfs config → memcopy into ring → enqueue handle
comes in the Acquisition round. For now it heartbeats so the supervisor has a
live process to monitor.
"""
from __future__ import annotations

import logging
import time

from . import metrics

log = logging.getLogger("frameforge.acquisition")


def run(cam, cfg, ring, data_q, drain, registry) -> None:
    log.info("[stub] acquisition %s starting", cam.id)
    n = 0
    while not drain.is_set():
        time.sleep(1.0)
        n += 1
        metrics.gauge(registry, "acq.%s.heartbeat" % cam.id, n)
    log.info("[stub] acquisition %s stopping (n=%d)", cam.id, n)
