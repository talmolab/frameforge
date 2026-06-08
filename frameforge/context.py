"""Shared worker dependencies, built once by the supervisor and passed to every
worker. Holds the live config, two drain Events, the metrics registry, and
the session_name (snapshotted at boot; per-day recording_start_str is
derived later by the encoder per chunk open).

Drain events follow the two-signal model from docs/deployment.md:
- ``drain`` is set on SIGTERM. Encoder finishes the current chunk and then
  exits. Acquisition keeps producing so the encoder has frames.
- ``hard_drain`` is set on SIGINT (also sets ``drain``). Every worker exits
  ASAP; encoder finalizes the partial .mp4 and stops.

The metrics registry splits internally by type (counters vs gauges) so the
Prometheus exporter knows how to emit each. All registry operations are
defensive — a metrics failure must never propagate into the acq/encode loop.
"""

import logging
from dataclasses import dataclass
from typing import Any

from .config import Config

_metrics_logger = logging.getLogger("frameforge.metrics")


class MetricsRegistry:
    """Two-dict (counters + gauges) registry shared across worker processes."""

    def __init__(self, counters_dict, gauges_dict) -> None:
        self._counters = counters_dict
        self._gauges = gauges_dict

    def incr(self, key: str, by: int = 1) -> None:
        try:
            self._counters[key] = self._counters.get(key, 0) + by
        except Exception as registry_error:
            _metrics_logger.warning("incr(%s) failed: %s", key, registry_error)

    def gauge(self, key: str, value) -> None:
        try:
            self._gauges[key] = value
        except Exception as registry_error:
            _metrics_logger.warning("gauge(%s) failed: %s", key, registry_error)

    def snapshot_counters(self) -> dict:
        try:
            return dict(self._counters)
        except Exception as registry_error:
            _metrics_logger.warning("snapshot_counters failed: %s", registry_error)
            return {}

    def snapshot_gauges(self) -> dict:
        try:
            return dict(self._gauges)
        except Exception as registry_error:
            _metrics_logger.warning("snapshot_gauges failed: %s", registry_error)
            return {}


@dataclass
class Context:
    config: Config
    drain: Any
    hard_drain: Any
    metrics: MetricsRegistry
    session_name: str
