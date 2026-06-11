"""Per-process + host-level stats sampler.

- Per-worker: proc_rss_mb{worker}, proc_cpu_user_seconds{worker}
- Host-level: host_mem_available_mb, host_load_avg_1m, host_cpu_busy_ratio
"""

import logging
import os
import time

from ..core.context import Context
from ..metrics.defs import (
    host_cpu_busy_ratio,
    host_load_avg_1m,
    host_mem_available_mb,
    proc_cpu_user_seconds,
    proc_rss_mb,
)


_SAMPLE_INTERVAL_S = 2.0
_CLOCK_TICKS_PER_S = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_BYTES_PER_MB = 1024 * 1024


class HostSampler:
    def __init__(self, context: Context, worker_pids) -> None:
        self.context = context
        self.worker_pids = worker_pids
        self.logger = logging.getLogger("frameforge.host_sampler")
        self._prev_cpu_ticks = None

    def run(self) -> None:
        self.logger.info(
            "host sampler starting (interval=%.1fs)", _SAMPLE_INTERVAL_S)
        while not self.context.drain.is_set():
            self._sample_all()
            self._sample_host()
            self._sleep_with_drain(_SAMPLE_INTERVAL_S)
        self.logger.info("host sampler stopping")

    def _sample_all(self):
        for worker_name, pid in list(self.worker_pids.items()):
            if not pid:
                continue
            rss_bytes = _read_rss_bytes(pid)
            if rss_bytes is not None:
                proc_rss_mb.labels(worker=worker_name).set(
                    round(rss_bytes / _BYTES_PER_MB, 4))
            cpu_user_seconds = _read_cpu_user_seconds(pid)
            if cpu_user_seconds is not None:
                proc_cpu_user_seconds.labels(worker=worker_name).set(
                    round(cpu_user_seconds, 4))

    def _sample_host(self):
        mem_available_bytes = _read_mem_available_bytes()
        if mem_available_bytes is not None:
            host_mem_available_mb.set(
                round(mem_available_bytes / _BYTES_PER_MB, 4))

        load_1m = _read_load_avg_1m()
        if load_1m is not None:
            host_load_avg_1m.set(round(load_1m, 4))

        cpu_ticks = _read_cpu_ticks()
        if cpu_ticks is not None:
            if self._prev_cpu_ticks is not None:
                prev_busy, prev_total = self._prev_cpu_ticks
                busy, total = cpu_ticks
                d_total = total - prev_total
                if d_total > 0:
                    d_busy = busy - prev_busy
                    host_cpu_busy_ratio.set(
                        round(max(0.0, min(1.0, d_busy / d_total)), 4))
            self._prev_cpu_ticks = cpu_ticks

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


def _read_mem_available_bytes():
    try:
        with open("/proc/meminfo") as meminfo_file:
            for line in meminfo_file:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _read_load_avg_1m():
    try:
        with open("/proc/loadavg") as loadavg_file:
            return float(loadavg_file.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_cpu_ticks():
    try:
        with open("/proc/stat") as stat_file:
            line = stat_file.readline()
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        ticks = [int(value) for value in parts[1:8]]
    except ValueError:
        return None
    total = sum(ticks)
    idle = ticks[3] + ticks[4]
    return total - idle, total
