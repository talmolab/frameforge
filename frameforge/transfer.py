"""SMB push worker (the Python ``robocopy``).

- Periodic scan of ``paths.scratch`` for finalized chunks (``*.mp4``, skipping
  ``*.part``). One upload at a time via ``smbprotocol``; serial is enough at
  one chunk per camera per hour.
- Credentials come from ``VAST_USER`` / ``VAST_PASS`` env vars (set by the
  systemd ``EnvironmentFile``). Never on disk in this repo.
- Session registered once and reused; on any upload error the session is
  marked dead and re-registered on the next scan.
- No internal retry loop: the periodic scan is the retry mechanism. After
  ``max_attempts_per_chunk`` scans for the same file, a one-shot ``STUCK``
  error log fires and a counter increments; the file stays on disk for human
  recovery.
- Low local disk → ERROR log + ``transfer.low_disk`` gauge; no auto-delete
  (acquisition's back-pressure is the natural brake).
"""

import logging
import os
import shutil
import time
from typing import Optional, Tuple

import smbclient

from .context import Context
from .log_utils import RateLimited


_METRIC_UPLOADED      = "transfer.uploaded"
_METRIC_FAILURES      = "transfer.failures"
_METRIC_STUCK         = "transfer.stuck"
_METRIC_FREE_MB       = "transfer.free_mb"
_METRIC_LOW_DISK      = "transfer.low_disk"
_METRIC_SESSION_ALIVE = "transfer.session_alive"

_SMB_PORT = 445
_UPLOAD_BUFFER_BYTES = 4 * 1024 * 1024
_DRAIN_POLL_INTERVAL_S = 0.5
_LOW_DISK_LOG_INTERVAL_S = 600.0
_SMB_FAIL_LOG_INTERVAL_S = 600.0


class Transfer:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.config = context.config.transfer
        self.scratch_dir = context.config.paths.scratch
        self.logger = logging.getLogger("frameforge.transfer")

        self._session_alive = False
        self._attempt_counts = {}
        self._stuck_already_logged = set()
        self._credentials: Optional[Tuple[str, str]] = None
        self._low_disk_limiter = RateLimited(_LOW_DISK_LOG_INTERVAL_S)
        self._smb_fail_limiter = RateLimited(_SMB_FAIL_LOG_INTERVAL_S)

    def run(self) -> None:
        self._credentials = self._load_credentials()
        self.logger.info(
            "transfer starting (target=//%s/%s/%s)",
            self.config.smb_server,
            self.config.smb_share,
            self.config.smb_root,
        )

        while not self.context.drain.is_set():
            if not self._session_alive:
                self._try_session()
            else:
                self._scan_and_upload()

            self._check_disk()
            self._sleep_with_drain(self.config.scan_interval_s)

        self._close_session()
        self.logger.info("transfer stopping")

    def _load_credentials(self):
        username = os.environ.get("VAST_USER")
        password = os.environ.get("VAST_PASS")
        if not username or not password:
            raise RuntimeError(
                "transfer: VAST_USER/VAST_PASS env vars are required")
        return (username, password)

    def _try_session(self):
        username, password = self._credentials
        try:
            smbclient.register_session(
                self.config.smb_server,
                username=username,
                password=password,
                port=_SMB_PORT,
            )
            self._session_alive = True
            self._smb_fail_limiter.reset()
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 1)
            self.logger.info(
                "SMB session registered server=%s", self.config.smb_server)
            return True
        except Exception as session_error:
            self._session_alive = False
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)
            if self._smb_fail_limiter.should_log():
                # Prometheus alert: transfer_session_alive == 0 for 5m
                self.logger.error(
                    "SMB session registration failed err=%s", session_error)
            return False

    def _close_session(self):
        try:
            smbclient.delete_session(self.config.smb_server)
        except Exception:
            pass
        self._session_alive = False
        self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)

    def _remote_root(self):
        return (
            "\\\\" + self.config.smb_server
            + "\\" + self.config.smb_share
            + "\\" + self.config.smb_root.replace("/", "\\")
        )

    def _local_to_remote(self, local_path):
        relative = os.path.relpath(local_path, self.scratch_dir)
        return self._remote_root() + "\\" + relative.replace(os.sep, "\\")

    def _find_finalized_chunks(self):
        finalized = []
        for directory, _subdirs, filenames in os.walk(self.scratch_dir):
            for filename in filenames:
                if filename.endswith(".mp4"):
                    finalized.append(os.path.join(directory, filename))
        finalized.sort()
        return finalized

    def _scan_and_upload(self):
        for local_path in self._find_finalized_chunks():
            if self.context.drain.is_set():
                return

            self._attempt_counts[local_path] = (
                self._attempt_counts.get(local_path, 0) + 1)
            try:
                self._upload_one(local_path)
            except Exception as upload_error:
                self._handle_upload_failure(local_path, upload_error)
                return

            self._handle_upload_success(local_path)

    def _upload_one(self, local_path):
        remote_path = self._local_to_remote(local_path)
        remote_dir = remote_path.rsplit("\\", 1)[0]
        smbclient.makedirs(remote_dir, exist_ok=True)
        with open(local_path, "rb") as local_file:
            with smbclient.open_file(remote_path, mode="wb") as remote_file:
                shutil.copyfileobj(
                    local_file, remote_file, length=_UPLOAD_BUFFER_BYTES)

    def _handle_upload_success(self, local_path):
        try:
            os.remove(local_path)
        except OSError:
            self.logger.exception("could not delete uploaded %s", local_path)
        self._attempt_counts.pop(local_path, None)
        self._stuck_already_logged.discard(local_path)
        self.context.metrics.incr(_METRIC_UPLOADED)
        self.logger.debug("uploaded path=%s", local_path)

    def _handle_upload_failure(self, local_path, error):
        self.context.metrics.incr(_METRIC_FAILURES)
        attempts = self._attempt_counts[local_path]
        max_attempts = self.config.max_attempts_per_chunk
        if attempts >= max_attempts:
            if local_path not in self._stuck_already_logged:
                # Prometheus alert: transfer_stuck > 0 for 10m
                self.logger.error(
                    "STUCK chunk attempts=%d path=%s err=%s",
                    attempts, local_path, error)
                self._stuck_already_logged.add(local_path)
                self.context.metrics.incr(_METRIC_STUCK)
        elif attempts == 1 or attempts % 5 == 0:
            self.logger.warning(
                "upload failed attempt=%d/%d path=%s err=%s",
                attempts, max_attempts, local_path, error)

        # Treat any error as a possibly-broken session; outer loop reconnects.
        self._session_alive = False
        self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)

    def _check_disk(self):
        try:
            disk_stat = os.statvfs(self.scratch_dir)
        except OSError:
            return
        free_mb = (disk_stat.f_bavail * disk_stat.f_frsize) // (1024 * 1024)
        threshold_mb = self.config.low_disk_threshold_mb
        # Prometheus alert: transfer_free_mb < <threshold>
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
