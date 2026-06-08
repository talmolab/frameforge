"""Local chunk reaper: analyzes finalized chunks, logs a summary, deletes.

Alternate to Transfer for isolated dev/testing (no SMB, no network).
Scans ``paths.scratch`` for finalized ``*.mp4`` chunks, runs
``ffprobe`` if installed (frames/duration/bitrate), logs a one-line
summary, then deletes the file.

No retries, no session, no upload. Delete failure → warn and continue;
the next scan will retry. Low local disk → ERROR + ``transfer.low_disk``
gauge (same metric name as Transfer so dashboards work in either mode).
"""

import logging
import os
import subprocess
import time
from typing import Dict

from .context import Context
from .log_utils import RateLimited


_METRIC_PROCESSED = "transfer.processed"
_METRIC_FREE_MB   = "transfer.free_mb"
_METRIC_LOW_DISK  = "transfer.low_disk"

_DRAIN_POLL_INTERVAL_S = 0.5
_LOW_DISK_LOG_INTERVAL_S = 600.0
_FFPROBE_TIMEOUT_S = 10.0


class LocalReaper:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.config = context.config.transfer
        self.scratch_dir = context.config.paths.scratch
        self.logger = logging.getLogger("frameforge.local_reaper")
        self._low_disk_limiter = RateLimited(_LOW_DISK_LOG_INTERVAL_S)

    def run(self) -> None:
        self.logger.info(
            "local_reaper starting scratch=%s", self.scratch_dir)
        while not self.context.drain.is_set():
            self._scan_and_reap()
            self._check_disk()
            self._sleep_with_drain(self.config.scan_interval_s)
        self.logger.info("local_reaper stopping")

    def _find_finalized_chunks(self):
        finalized = []
        for directory, _subdirs, filenames in os.walk(self.scratch_dir):
            for filename in filenames:
                if filename.endswith(".mp4"):
                    finalized.append(os.path.join(directory, filename))
        finalized.sort()
        return finalized

    def _scan_and_reap(self):
        for local_path in self._find_finalized_chunks():
            if self.context.drain.is_set():
                return
            info = self._analyze(local_path)
            self._log_summary(local_path, info)
            try:
                os.remove(local_path)
            except OSError as remove_error:
                self.logger.warning(
                    "could not delete %s err=%s", local_path, remove_error)
                continue
            self.context.metrics.incr(_METRIC_PROCESSED)

    def _analyze(self, path) -> Dict[str, str]:
        info: Dict[str, str] = {}
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:
            return info
        info["size_mb"] = "%.2f" % size_mb

        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries",
                 "stream=nb_frames,duration,avg_frame_rate,codec_name",
                 "-of", "csv=p=0",
                 path],
                capture_output=True, text=True,
                timeout=_FFPROBE_TIMEOUT_S, check=True,
            )
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            return info

        parts = result.stdout.strip().split(",")
        if len(parts) < 4:
            return info
        codec, frames, duration, fps = parts[:4]
        info["codec"] = codec or "?"
        info["frames"] = frames or "?"
        info["duration_s"] = duration or "?"
        info["fps"] = fps or "?"
        try:
            duration_f = float(duration)
            if duration_f > 0:
                info["bitrate_mbps"] = "%.2f" % ((size_mb * 8) / duration_f)
        except ValueError:
            pass
        return info

    def _log_summary(self, path, info):
        relative = os.path.relpath(path, self.scratch_dir)
        info_str = " ".join("%s=%s" % (key, value) for key, value in info.items())
        self.logger.info("reaped path=%s %s", relative, info_str)

    def _check_disk(self):
        try:
            disk_stat = os.statvfs(self.scratch_dir)
        except OSError:
            return
        free_mb = (disk_stat.f_bavail * disk_stat.f_frsize) // (1024 * 1024)
        threshold_mb = self.config.low_disk_threshold_mb
        self.context.metrics.gauge(_METRIC_FREE_MB, free_mb)
        if free_mb < threshold_mb:
            if self._low_disk_limiter.should_log():
                self.logger.error(
                    "LOW DISK free_mb=%d threshold_mb=%d path=%s",
                    free_mb, threshold_mb, self.scratch_dir)
            self.context.metrics.gauge(_METRIC_LOW_DISK, 1)
        else:
            self._low_disk_limiter.reset()
            self.context.metrics.gauge(_METRIC_LOW_DISK, 0)

    def _sleep_with_drain(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.context.drain.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, _DRAIN_POLL_INTERVAL_S))
