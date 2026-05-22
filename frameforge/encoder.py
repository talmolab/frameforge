"""Encoder worker (one per camera).

STUB: real HW-encode (nvv4l2h264enc / PyAV) + frame-count chunk rotation +
atomic .part→.mp4 finalize + black-frame fill comes in the Encoder round.
On drain it must finalize the current chunk promptly (no mid-write kill).
"""
from __future__ import annotations

import logging
import time

from . import metrics

log = logging.getLogger("frameforge.encoder")


def run(cam, cfg, ring, data_q, drain, registry) -> None:
    log.info("[stub] encoder %s starting", cam.id)
    n = 0
    while not drain.is_set():
        time.sleep(1.0)
        n += 1
        metrics.gauge(registry, "enc.%s.heartbeat" % cam.id, n)
    log.info("[stub] encoder %s finalizing + stopping (n=%d)", cam.id, n)
