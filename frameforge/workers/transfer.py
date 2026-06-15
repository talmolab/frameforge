"""SMB push worker: scan scratch, optionally analyze, upload to VAST, delete."""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field

from ..core.context import Context
from ..media.smb_session import SmbSession
from ..metrics.helpers import RateLimited
from ..metrics.defs import (
    transfer_failures,
    transfer_free_mb,
    transfer_low_disk,
    transfer_stuck,
    transfer_uploaded,
)

_DRAIN_POLL_INTERVAL_S = 0.5
_LOW_DISK_LOG_INTERVAL_S = 600.0
_FFPROBE_TIMEOUT_S = 10.0
_MAX_UPLOAD_ATTEMPTS = 30


@dataclass(slots=True)
class UploadState:
    attempt_counts: dict[str, int] = field(default_factory=dict)
    stuck_already_logged: set[str] = field(default_factory=set)

    def attempt(self, path: str) -> int:
        self.attempt_counts[path] = self.attempt_counts.get(path, 0) + 1
        return self.attempt_counts[path]

    def succeed(self, path: str) -> None:
        self.attempt_counts.pop(path, None)
        self.stuck_already_logged.discard(path)

    def mark_stuck(self, path: str) -> bool:
        if path in self.stuck_already_logged:
            return False
        self.stuck_already_logged.add(path)
        return True


class Transfer:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.config = context.config.transfer
        self.scratch_dir = context.config.scratch_dir
        self.logger = logging.getLogger("frameforge.transfer")

        self.session = SmbSession(
            server=self.config.smb_server,
            share=self.config.smb_share,
            root=self.config.smb_root,
            scratch_dir=self.scratch_dir,
        )
        self._uploads = UploadState()
        self._low_disk_limiter = RateLimited(_LOW_DISK_LOG_INTERVAL_S)

    def run(self) -> None:
        self.logger.info(
            "transfer starting (target=//%s/%s/%s analytics=%s)",
            self.config.smb_server,
            self.config.smb_share,
            self.config.smb_root,
            self.config.analytics,
        )

        while not self.context.drain.is_set():
            if self.session.ensure_open():
                self._scan_and_upload()
            self._check_disk()
            self._sleep_with_drain(self.config.scan_interval_s)

        self.session.close()
        self.logger.info("transfer stopping")

    def _scan_and_upload(self) -> None:
        for local_path in self._find_finalized_chunks():
            if self.context.drain.is_set():
                return

            if self.config.analytics:
                info = self._analyze(local_path)
                relative = os.path.relpath(local_path, self.scratch_dir)
                info_str = " ".join(
                    "%s=%s" % (key, value) for key, value in info.items())
                self.logger.info("chunk path=%s %s", relative, info_str)

            attempts = self._uploads.attempt(local_path)
            try:
                self.session.upload(local_path)
            except Exception as upload_error:
                transfer_failures.inc()
                if attempts >= _MAX_UPLOAD_ATTEMPTS:
                    if self._uploads.mark_stuck(local_path):
                        self.logger.error(
                            "STUCK chunk attempts=%d path=%s err=%s",
                            attempts, local_path, upload_error)
                        transfer_stuck.inc()
                elif attempts == 1 or attempts % 5 == 0:
                    self.logger.warning(
                        "upload failed attempt=%d/%d path=%s err=%s",
                        attempts, _MAX_UPLOAD_ATTEMPTS, local_path, upload_error)
                self.session.mark_dead()
                return

            try:
                os.remove(local_path)
            except OSError:
                self.logger.exception("could not delete uploaded %s", local_path)
            self._uploads.succeed(local_path)
            transfer_uploaded.inc()
            self.logger.debug("uploaded path=%s", local_path)

    def _find_finalized_chunks(self):
        finalized = []
        for directory, _subdirs, filenames in os.walk(self.scratch_dir):
            for filename in filenames:
                if filename.endswith(".mp4"):
                    finalized.append(os.path.join(directory, filename))
        finalized.sort()
        return finalized

    def _analyze(self, path) -> dict[str, str]:
        info: dict[str, str] = {}
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

    def _check_disk(self):
        try:
            disk_stat = os.statvfs(self.scratch_dir)
        except OSError:
            return

        free_mb = (disk_stat.f_bavail * disk_stat.f_frsize) // (1024 * 1024)
        threshold_mb = self.config.low_disk_threshold_mb
        transfer_free_mb.set(free_mb)

        if free_mb < threshold_mb:
            if self._low_disk_limiter.should_log():
                self.logger.error(
                    "LOW DISK free_mb=%d threshold_mb=%d path=%s",
                    free_mb, threshold_mb, self.scratch_dir)
            transfer_low_disk.set(1)
        else:
            self._low_disk_limiter.reset()
            transfer_low_disk.set(0)

    def _sleep_with_drain(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.context.drain.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, _DRAIN_POLL_INTERVAL_S))
