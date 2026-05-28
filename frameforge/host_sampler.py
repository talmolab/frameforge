"""Per-process host stats sampler.

- Reads ``/proc/<pid>/{status,stat}`` for each worker pid registered by the
  supervisor (Manager dict, shared across processes).
- Writes gauges into the same MetricsRegistry the exporter reads:
    proc.<worker>.rss_bytes
    proc.<worker>.cpu_user_seconds
- Stale pids (worker died, supervisor hasn't yet updated the entry) are
  swallowed silently — the next tick will catch the new pid.
- Linux-only by design; ``/proc`` parsing is portable to JetPack 4.6.
"""

import logging
import os
import time

from .context import Context


_METRIC_RSS = "proc.%s.rss_bytes"
_METRIC_CPU_USER = "proc.%s.cpu_user_seconds"

_SAMPLE_INTERVAL_S = 2.0
_CLOCK_TICKS_PER_S = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


class HostSampler:
    def __init__(self, context: Context, worker_pids) -> None:
        self.context = context
        self.worker_pids = worker_pids
        self.logger = logging.getLogger("frameforge.host_sampler")

    def run(self) -> None:
        self.logger.info(
            "host sampler starting (interval=%.1fs)", _SAMPLE_INTERVAL_S)
        while not self.context.drain.is_set():
            self._sample_all()
            self._sleep_with_drain(_SAMPLE_INTERVAL_S)
        self.logger.info("host sampler stopping")

    def _sample_all(self):
        for worker_name, pid in list(self.worker_pids.items()):
            if not pid:
                continue
            rss_bytes = _read_rss_bytes(pid)
            if rss_bytes is not None:
                self.context.metrics.gauge(_METRIC_RSS % worker_name, rss_bytes)
            cpu_user_seconds = _read_cpu_user_seconds(pid)
            if cpu_user_seconds is not None:
                self.context.metrics.gauge(
                    _METRIC_CPU_USER % worker_name, cpu_user_seconds)

    def _sleep_with_drain(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.context.drain.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.5))


def _read_rss_bytes(pid):
    try:
        with open("/proc/%d/status" % pid) as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _read_cpu_user_seconds(pid):
    try:
        with open("/proc/%d/stat" % pid) as stat_file:
            raw = stat_file.read()
    except OSError:
        return None
    try:
        after_comm = raw.rsplit(")", 1)[1].split()
        utime_ticks = int(after_comm[11])
        return utime_ticks / _CLOCK_TICKS_PER_S
    except (IndexError, ValueError):
        return None
