"""Single source of truth for prom_client metric objects.

Multi-process mode: workers write to mmap files under
``$PROMETHEUS_MULTIPROC_DIR``; the exporter aggregates via
``MultiProcessCollector``. Set ``PROMETHEUS_MULTIPROC_DIR`` before this
module is first imported (handled in ``__main__.py``).
"""

import logging

from prometheus_client import Counter, Gauge, Histogram

from .helpers import BurstAggregator, RECURRING_WARN_INTERVAL_S


_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.5, 1.0, 5.0)


# Bundles Counter + BurstAggregator + log emission so worker call sites
# stay a single `.inc(latest=..., cam=...)` line, per label combination.
class BurstCounter:
    def __init__(self, name: str, labels, documentation: str,
                 logger_name: str, message: str,
                 *, interval_s: float = RECURRING_WARN_INTERVAL_S) -> None:
        self._counter = Counter(name, documentation, labels)
        self._labels = list(labels)
        self._logger = logging.getLogger(logger_name)
        self._message = message
        self._interval_s = interval_s
        self._aggregators = {}

    def inc(self, latest=None, **label_values) -> None:
        key = tuple(label_values[name] for name in self._labels)
        aggregator = self._aggregators.get(key)
        if aggregator is None:
            aggregator = BurstAggregator(self._interval_s)
            self._aggregators[key] = aggregator

        self._counter.labels(**label_values).inc()

        snapshot = aggregator.event(latest=latest)
        if snapshot is None:
            return

        count, elapsed_s, payload = snapshot
        label_vals = [label_values[name] for name in self._labels]
        if payload is not None:
            self._logger.warning(
                self._message, *label_vals, count, elapsed_s, *payload)
        else:
            self._logger.warning(
                self._message, *label_vals, count, elapsed_s)


# --- Acquisition ---------------------------------------------------------
acq_incomplete = BurstCounter(
    "acq_incomplete", ["cam"],
    "Frames the camera reported as incomplete",
    logger_name="frameforge.acquisition",
    message="incomplete frames cam=%s count=%d in_last=%ds code=%s msg=%s")
acq_overrun_drops = BurstCounter(
    "acq_overrun_drops", ["cam"],
    "Frames dropped because the frame ring was full",
    logger_name="frameforge.acquisition",
    message="ring full, dropping frames cam=%s count=%d in_last=%ds")
acq_missed_frames = BurstCounter(
    "acq_missed_frames", ["cam"],
    "Frames the camera sent that never reached the acq loop (BlockID gap)",
    logger_name="frameforge.acquisition",
    message="missed frames cam=%s count=%d in_last=%ds gap=%d")
acq_loop_ms_last = Gauge(
    "acq_loop_ms_last", "Last sampled grab-loop wall time (ms)",
    ["cam"], multiprocess_mode="livesum")
acq_loop_ms_max = Gauge(
    "acq_loop_ms_max", "Sampled-window max grab-loop wall time (ms)",
    ["cam"], multiprocess_mode="livesum")
acq_queue_depth = Gauge(
    "acq_queue_depth", "Current frame-data queue depth",
    ["cam"], multiprocess_mode="livesum")
acq_ring_free = Gauge(
    "acq_ring_free", "Free slots remaining in the frame ring",
    ["cam"], multiprocess_mode="livesum")


# --- Encoder -------------------------------------------------------------
enc_writer_failures = Counter(
    "enc_writer_failures", "Encoder backend write failures",
    ["cam"])
enc_open_failures = Counter(
    "enc_open_failures", "Encoder backend open failures",
    ["cam"])
enc_idle = Gauge(
    "enc_idle", "1 when encoder is idling until the next chunk boundary",
    ["cam"], multiprocess_mode="livesum")
enc_encode_ms_last = Gauge(
    "enc_encode_ms_last", "Last sampled per-frame encode wall time (ms)",
    ["cam"], multiprocess_mode="livesum")
enc_encode_ms_max = Gauge(
    "enc_encode_ms_max", "Sampled-window max per-frame encode wall time (ms)",
    ["cam"], multiprocess_mode="livesum")
enc_encode_duration_seconds = Histogram(
    "enc_encode_duration_seconds", "Per-frame encoder write wall time",
    ["cam"], buckets=_LATENCY_BUCKETS)


# --- Broadcast -----------------------------------------------------------
bcast_session_alive = Gauge(
    "bcast_session_alive", "1 when broadcast pipeline is open, 0 when down",
    ["cam"], multiprocess_mode="livesum")
bcast_dropped = Counter(
    "bcast_dropped", "Broadcast-ring overruns (producer-side drops)",
    ["cam"])
bcast_encode_ms_last = Gauge(
    "bcast_encode_ms_last", "Last sampled per-frame broadcast encode time (ms)",
    ["cam"], multiprocess_mode="livesum")
bcast_encode_ms_max = Gauge(
    "bcast_encode_ms_max", "Sampled-window max broadcast encode time (ms)",
    ["cam"], multiprocess_mode="livesum")
bcast_encode_duration_seconds = Histogram(
    "bcast_encode_duration_seconds", "Per-frame broadcast encode wall time",
    ["cam"], buckets=_LATENCY_BUCKETS)


# --- Transfer ------------------------------------------------------------
transfer_session_alive = Gauge(
    "transfer_session_alive", "1 when SMB session is registered, 0 when down",
    multiprocess_mode="livesum")
transfer_uploaded = Counter(
    "transfer_uploaded", "Chunks successfully uploaded to VAST")
transfer_failures = Counter(
    "transfer_failures", "Per-attempt upload failures")
transfer_stuck = Counter(
    "transfer_stuck", "Chunks that exceeded max upload attempts")
transfer_free_mb = Gauge(
    "transfer_free_mb", "Free space in scratch directory (MB)",
    multiprocess_mode="livesum")
transfer_low_disk = Gauge(
    "transfer_low_disk", "1 when scratch free space is below threshold",
    multiprocess_mode="livesum")


# --- Supervisor ----------------------------------------------------------
worker_restarts = Counter(
    "worker_restarts", "Worker process restart events",
    ["worker"])
soft_drain_pending = Gauge(
    "soft_drain_pending", "1 while supervisor is in soft drain (waiting for chunk boundary)",
    multiprocess_mode="livesum")


# --- Host sampler --------------------------------------------------------
proc_rss_mb = Gauge(
    "proc_rss_mb", "Resident set size per worker (MB)",
    ["worker"], multiprocess_mode="livesum")
proc_cpu_user_seconds = Gauge(
    "proc_cpu_user_seconds", "Cumulative user CPU per worker (seconds)",
    ["worker"], multiprocess_mode="livesum")
host_mem_available_mb = Gauge(
    "host_mem_available_mb", "System memory available (MB)",
    multiprocess_mode="livesum")
host_load_avg_1m = Gauge(
    "host_load_avg_1m", "1-minute system load average",
    multiprocess_mode="livesum")
host_cpu_busy_ratio = Gauge(
    "host_cpu_busy_ratio",
    "Whole-machine CPU busy fraction (0-1) over last sample interval",
    multiprocess_mode="livesum")
