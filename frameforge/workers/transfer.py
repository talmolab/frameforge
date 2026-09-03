"""Upload worker: scan scratch, optionally analyze, push to storage, delete."""

import logging
import os
import posixpath
import random
import subprocess
from dataclasses import dataclass, field

from ..context import Context
from ..core.logging_setup import DEDUP_INTERVAL_S, DEDUP_KEY
from ..core.paths import SCRATCH_DIR
from ..encoding.chunk_scheduler import sidecar_for
from ..metrics.defs import (
    transfer_discarded,
    transfer_failures,
    transfer_free_mb,
    transfer_low_disk,
    transfer_session_alive,
    transfer_session_prefix,
    transfer_uploaded,
)
from ..storage import make_storage

_LOW_DISK_THRESHOLD_MB = 500
_LOW_DISK_LOG_INTERVAL_S = 300.0
_FFPROBE_TIMEOUT_S = 10.0
_MAX_UPLOAD_ATTEMPTS = 30
_SCAN_INTERVAL_S = 30.0
_SCAN_JITTER_S = 10.0


@dataclass(slots=True)
class UploadState:
    attempt_counts: dict[str, int] = field(default_factory=dict)

    def attempt(self, path: str) -> int:
        self.attempt_counts[path] = self.attempt_counts.get(path, 0) + 1
        return self.attempt_counts[path]

    def clear(self, path: str) -> None:
        self.attempt_counts.pop(path, None)


class Transfer:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.transfer_config = context.config.transfer
        self.scratch_dir = SCRATCH_DIR
        self.logger = logging.getLogger("frameforge.transfer")

        self.storage = make_storage(self.transfer_config.storage)
        self._uploads = UploadState()

    def run(self) -> None:
        prefix = posixpath.join(self.storage.location, self.context.session_name)
        transfer_session_prefix.labels(prefix=prefix).set(1)

        self.logger.info(
            "transfer starting (target=%s analytics=%s)",
            prefix, self.transfer_config.analytics)

        while not self.context.hard_drain.is_set():
            if self._ensure_open():
                self._scan_and_upload()
            self._check_disk()
            self.context.hard_drain.wait(
                _SCAN_INTERVAL_S + random.uniform(0, _SCAN_JITTER_S))

        self.storage.close()
        transfer_session_alive.set(0)
        self.logger.info("transfer stopping")

    def _ensure_open(self) -> bool:
        is_open = self.storage.ensure_open()
        transfer_session_alive.set(1 if is_open else 0)
        return is_open

    def _mark_dead(self) -> None:
        self.storage.mark_dead()
        transfer_session_alive.set(0)

    def _put(self, local_path: str) -> None:
        relative = os.path.relpath(local_path, self.scratch_dir)
        self.storage.put(local_path, relative.replace(os.sep, "/"))

    def _scan_and_upload(self) -> None:
        for local_path in self._find_finalized_chunks():
            if self.context.hard_drain.is_set():
                return

            if self.transfer_config.analytics:
                info = self._analyze(local_path)
                relative = os.path.relpath(local_path, self.scratch_dir)
                info_str = " ".join(
                    "%s=%s" % (key, value) for key, value in info.items())
                self.logger.info("chunk path=%s %s", relative, info_str)

            sidecar_path = sidecar_for(local_path)
            has_sidecar = os.path.exists(sidecar_path)

            if has_sidecar:
                try:
                    self._put(sidecar_path)
                except Exception as sidecar_error:
                    self.logger.warning(
                        "sidecar upload failed path=%s err=%s (mp4 continues)",
                        sidecar_path, sidecar_error,
                        extra={DEDUP_KEY: ("sidecar_fail", sidecar_path)})
                    has_sidecar = False

            attempts = self._uploads.attempt(local_path)
            try:
                self._put(local_path)
            except Exception as upload_error:
                transfer_failures.inc()
                if attempts >= _MAX_UPLOAD_ATTEMPTS:
                    self.logger.error(
                        "DISCARDING chunk attempts=%d path=%s err=%s",
                        attempts, local_path, upload_error)
                    try:
                        os.remove(local_path)
                    except OSError:
                        self.logger.exception(
                            "could not delete unrecoverable %s", local_path)
                    self._uploads.clear(local_path)
                    transfer_discarded.inc()
                else:
                    self.logger.warning(
                        "upload failed attempt=%d/%d path=%s err=%s",
                        attempts, _MAX_UPLOAD_ATTEMPTS, local_path, upload_error,
                        extra={DEDUP_KEY: ("upload_fail", local_path)})
                self._mark_dead()
                return

            try:
                os.remove(local_path)
            except OSError:
                self.logger.exception("could not delete uploaded %s", local_path)
            if has_sidecar:
                try:
                    os.remove(sidecar_path)
                except OSError:
                    self.logger.exception(
                        "could not delete uploaded sidecar %s", sidecar_path)
            self._uploads.clear(local_path)
            transfer_uploaded.inc()
            self.logger.debug("uploaded path=%s sidecar=%s",
                              local_path, has_sidecar)

    def _find_finalized_chunks(self):
        finalized = []
        for directory, _, filenames in os.walk(self.scratch_dir):
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
        return info

    def _check_disk(self):
        try:
            disk_stat = os.statvfs(self.scratch_dir)
        except OSError:
            return

        free_mb = (disk_stat.f_bavail * disk_stat.f_frsize) // (1024 * 1024)
        transfer_free_mb.set(free_mb)

        if free_mb < _LOW_DISK_THRESHOLD_MB:
            self.logger.error(
                "LOW DISK free_mb=%d threshold_mb=%d path=%s",
                free_mb, _LOW_DISK_THRESHOLD_MB, self.scratch_dir,
                extra={DEDUP_KEY: "transfer_low_disk",
                       DEDUP_INTERVAL_S: _LOW_DISK_LOG_INTERVAL_S})
            transfer_low_disk.set(1)
        else:
            transfer_low_disk.set(0)
