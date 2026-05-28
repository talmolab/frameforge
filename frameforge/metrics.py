"""Prometheus exporter (HTTP /metrics on :metrics_port).

- A custom collector reads the supervisor's shared MetricsRegistry on every
  scrape, parses each key into (metric_name, labels) per a small schema, and
  yields a Counter or Gauge family. Scrape work is defensive — metric errors
  are logged but never propagate.
- Key → metric schema:
    acq.<cam_id>.<name>          -> acq_<name>_total{cam=<cam_id>}
    enc.<cam_id>.<name>          -> enc_<name>_total{cam=<cam_id>}
    transfer.<name>              -> transfer_<name>_total | transfer_<name>
    worker_restarts.<worker>     -> worker_restarts_total{worker=<worker>}
"""

import logging
import time

from prometheus_client import (
    CollectorRegistry,
    PROCESS_COLLECTOR,
    start_http_server,
)
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
)

from . import __version__
from .context import Context


_BUILD_INFO = "frameforge_build_info"
_UPTIME_SECONDS = "frameforge_uptime_seconds"

_METRICS_PORT = 9100
_DRAIN_POLL_INTERVAL_S = 1.0


class Metrics:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.logger = logging.getLogger("frameforge.metrics")
        self._started_at_monotonic = time.monotonic()

    def run(self) -> None:
        registry = CollectorRegistry()
        registry.register(PROCESS_COLLECTOR)
        registry.register(_RegistryCollector(self.context, self._started_at_monotonic))

        start_http_server(_METRICS_PORT, addr="0.0.0.0", registry=registry)
        self.logger.info(
            "metrics exporter listening on :%d/metrics", _METRICS_PORT)

        while not self.context.drain.is_set():
            time.sleep(_DRAIN_POLL_INTERVAL_S)

        self.logger.info("metrics exporter stopping")


class _RegistryCollector:
    def __init__(self, context: Context, started_at_monotonic: float) -> None:
        self.context = context
        self._started_at_monotonic = started_at_monotonic
        self.logger = logging.getLogger("frameforge.metrics")

    def collect(self):
        try:
            yield from self._collect_meta()
            yield from self._collect_kind(
                self.context.metrics.snapshot_counters(), is_counter=True)
            yield from self._collect_kind(
                self.context.metrics.snapshot_gauges(), is_counter=False)
        except Exception as scrape_error:
            self.logger.warning("scrape failed: %s", scrape_error)

    def _collect_meta(self):
        build_info = GaugeMetricFamily(
            _BUILD_INFO, "frameforge build info", labels=["version"])
        build_info.add_metric([__version__], 1.0)
        yield build_info

        uptime_seconds = time.monotonic() - self._started_at_monotonic
        uptime = GaugeMetricFamily(
            _UPTIME_SECONDS, "Seconds since the metrics worker started")
        uptime.add_metric([], uptime_seconds)
        yield uptime

    def _collect_kind(self, snapshot, is_counter):
        families = {}
        for key, value in snapshot.items():
            try:
                name, label_names, label_values = _key_to_metric(key, is_counter)
            except Exception as parse_error:
                self.logger.warning("key parse failed (%s): %s", key, parse_error)
                continue

            family = families.get(name)
            if family is None:
                if is_counter:
                    family = CounterMetricFamily(name, "", labels=label_names)
                else:
                    family = GaugeMetricFamily(name, "", labels=label_names)
                families[name] = family
            family.add_metric(label_values, value)

        for family in families.values():
            yield family


def _key_to_metric(key, is_counter):
    parts = key.split(".")

    if len(parts) == 3 and parts[1].startswith("cam_"):
        family, cam_id, leaf = parts
        name = "%s_%s" % (family, leaf)
        label_names, label_values = ["cam"], [cam_id]

    elif len(parts) == 3 and parts[0] == "proc":
        name = "proc_" + parts[2]
        label_names, label_values = ["worker"], [parts[1]]

    elif len(parts) == 2 and parts[0] == "worker_restarts":
        name = "worker_restarts"
        label_names, label_values = ["worker"], [parts[1]]

    elif len(parts) == 2:
        name = "%s_%s" % (parts[0], parts[1])
        label_names, label_values = [], []

    else:
        name = key.replace(".", "_")
        label_names, label_values = [], []

    if is_counter:
        name = name + "_total"
    return name, label_names, label_values
