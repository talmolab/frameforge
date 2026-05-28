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


_METRIC_UPLOADED      = "transfer.uploaded"
_METRIC_FAILURES      = "transfer.failures"
_METRIC_STUCK         = "transfer.stuck"
_METRIC_FREE_BYTES    = "transfer.free_bytes"
_METRIC_LOW_DISK      = "transfer.low_disk"
_METRIC_SESSION_ALIVE = "transfer.session_alive"

_SMB_PORT = 445
_UPLOAD_BUFFER_BYTES = 4 * 1024 * 1024
_DRAIN_POLL_INTERVAL_S = 0.5


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
                if self._try_session():
                    self._ensure_remote_tree()
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
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 1)
            self.logger.info(
                "SMB session registered to %s", self.config.smb_server)
            return True
        except Exception as session_error:
            self._session_alive = False
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)
            self.logger.error(
                "SMB session registration failed: %s", session_error)
            return False

    def _close_session(self):
        try:
            smbclient.delete_session(self.config.smb_server)
        except Exception:
            pass
        self._session_alive = False
        self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)

    def _ensure_remote_tree(self):
        for camera in self.context.config.cameras:
            remote_dir = self._remote_camera_dir(camera.id)
            try:
                smbclient.makedirs(remote_dir, exist_ok=True)
            except Exception as makedirs_error:
                self.logger.warning(
                    "remote makedirs failed (%s): %s",
                    remote_dir, makedirs_error)

    def _remote_root(self):
        return (
            "\\\\" + self.config.smb_server
            + "\\" + self.config.smb_share
            + "\\" + self.config.smb_root.replace("/", "\\")
        )

    def _remote_camera_dir(self, camera_id):
        return "\\".join([
            self._remote_root(),
            self.context.session_name,
            camera_id,
            self.context.recording_start_str,
        ])

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
        self.logger.info("uploaded %s", local_path)

    def _handle_upload_failure(self, local_path, error):
        self.context.metrics.incr(_METRIC_FAILURES)
        attempts = self._attempt_counts[local_path]
        if attempts >= self.config.max_attempts_per_chunk:
            if local_path not in self._stuck_already_logged:
                self.logger.error(
                    "STUCK chunk after %d attempts: %s (%s)",
                    attempts, local_path, error)
                self._stuck_already_logged.add(local_path)
                self.context.metrics.incr(_METRIC_STUCK)
        else:
            self.logger.warning(
                "upload failed (attempt %d/%d): %s (%s)",
                attempts, self.config.max_attempts_per_chunk,
                local_path, error)

        # Treat any error as a possibly-broken session; outer loop reconnects.
        self._session_alive = False
        self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)

    def _check_disk(self):
        try:
            disk_stat = os.statvfs(self.scratch_dir)
        except OSError:
            return
        free_bytes = disk_stat.f_bavail * disk_stat.f_frsize
        threshold_bytes = self.config.low_disk_threshold_mb * 1024 * 1024
        self.context.metrics.gauge(_METRIC_FREE_BYTES, free_bytes)
        if free_bytes < threshold_bytes:
            self.logger.error(
                "LOW DISK: %d bytes free on %s (threshold=%d MB)",
                free_bytes, self.scratch_dir,
                self.config.low_disk_threshold_mb)
            self.context.metrics.gauge(_METRIC_LOW_DISK, 1)
        else:
            self.context.metrics.gauge(_METRIC_LOW_DISK, 0)

    def _sleep_with_drain(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.context.drain.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, _DRAIN_POLL_INTERVAL_S))
