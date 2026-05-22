"""Metrics process + shared registry helpers.

STUB: real Prometheus exporter comes in the Metrics round. The shared registry
is a ``Manager().dict`` written by all workers and served here.

Note: ``incr`` on a Manager dict is not atomic (read-modify-write); fine for
low-rate counters. Hot per-frame counters should move to ``mp.Value`` later.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("frameforge.metrics")


def gauge(registry, key: str, value) -> None:
    registry[key] = value


def incr(registry, key: str, by: int = 1) -> None:
    registry[key] = registry.get(key, 0) + by


def run(cfg, registry, drain) -> None:
    log.info("[stub] metrics exporter starting (will serve :%d)", cfg.metrics_port)
    while not drain.is_set():
        time.sleep(5.0)
        log.debug("[stub] metrics snapshot: %s", dict(registry))
    log.info("[stub] metrics exporter stopping")
