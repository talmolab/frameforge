"""Prometheus exporter (HTTP /metrics on :9100).

Multi-process aggregation: each worker writes to mmap files under
``$PROMETHEUS_MULTIPROC_DIR``; ``MultiProcessCollector`` aggregates them
at scrape time. Build info + uptime are exposed via a tiny custom
collector. ``PlatformCollector`` adds Python version info.
"""

import logging
import time

from prometheus_client import (
    CollectorRegistry,
    PlatformCollector,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.multiprocess import MultiProcessCollector

from .. import __version__
from ..context import Context


_BUILD_INFO = "frameforge_build_info"
_UPTIME_SECONDS = "frameforge_uptime_seconds"

_METRICS_PORT = 9100


class Metrics:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.logger = logging.getLogger("frameforge.metrics")
        self._started_at_monotonic = time.monotonic()

    def run(self) -> None:
        registry = CollectorRegistry()
        MultiProcessCollector(registry)
        PlatformCollector(registry=registry)
        registry.register(_MetaCollector(self._started_at_monotonic))

        start_http_server(_METRICS_PORT, addr="0.0.0.0", registry=registry)
        self.logger.info(
            "metrics exporter listening on :%d/metrics (multi-process)",
            _METRICS_PORT)

        self.context.hard_drain.wait()

        self.logger.info("metrics exporter stopping")


class _MetaCollector:
    def __init__(self, started_at_monotonic: float) -> None:
        self._started_at_monotonic = started_at_monotonic

    def collect(self):
        build_info = GaugeMetricFamily(
            _BUILD_INFO, "frameforge build info", labels=["version"])
        build_info.add_metric([__version__], 1.0)
        yield build_info

        uptime = GaugeMetricFamily(
            _UPTIME_SECONDS, "Seconds since the metrics exporter started")
        uptime.add_metric([], time.monotonic() - self._started_at_monotonic)
        yield uptime
