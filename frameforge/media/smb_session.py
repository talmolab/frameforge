"""SMB session: credentials, register, path mapping, upload, mark-dead, close.

Pulled out of Transfer so the upload worker stays focused on scanning and
retry state. Owns the ``transfer_session_alive`` gauge.
"""

import logging
import os
import shutil

import smbclient

from ..core.logging_setup import DEDUP_INTERVAL_S, DEDUP_KEY
from ..metrics.defs import transfer_session_alive

_SMB_PORT = 445
_SMB_FAIL_LOG_INTERVAL_S = 600.0
_UPLOAD_BUFFER_BYTES = 4 * 1024 * 1024


class SmbSession:
    def __init__(self, *, server: str, share: str, root: str,
                 scratch_dir: str) -> None:
        self.server = server
        self.share = share
        self.root = root
        self.scratch_dir = scratch_dir
        self.logger = logging.getLogger("frameforge.smb_session")

        self._username = os.environ.get("VAST_USER")
        self._password = os.environ.get("VAST_PASS")
        if not self._username or not self._password:
            raise RuntimeError(
                "smb_session: VAST_USER/VAST_PASS env vars are required")

        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def ensure_open(self) -> bool:
        if self._alive:
            return True

        try:
            smbclient.register_session(
                self.server,
                username=self._username,
                password=self._password,
                port=_SMB_PORT,
            )
        except Exception as session_error:
            self._alive = False
            transfer_session_alive.set(0)
            self.logger.error(
                "SMB session registration failed err=%s", session_error,
                extra={DEDUP_KEY: "smb_register_fail",
                       DEDUP_INTERVAL_S: _SMB_FAIL_LOG_INTERVAL_S})
            return False

        self._alive = True
        transfer_session_alive.set(1)
        self.logger.info("SMB session registered server=%s", self.server)
        return True

    def upload(self, local_path: str) -> None:
        remote_path = self._local_to_remote(local_path)
        remote_dir = remote_path.rsplit("\\", 1)[0]

        smbclient.makedirs(remote_dir, exist_ok=True)
        with open(local_path, "rb") as local_file:
            with smbclient.open_file(remote_path, mode="wb") as remote_file:
                shutil.copyfileobj(
                    local_file, remote_file, length=_UPLOAD_BUFFER_BYTES)

    def mark_dead(self) -> None:
        self._alive = False
        transfer_session_alive.set(0)

    def close(self) -> None:
        try:
            smbclient.delete_session(self.server)
        except Exception:
            pass
        self.mark_dead()

    def _local_to_remote(self, local_path: str) -> str:
        relative = os.path.relpath(local_path, self.scratch_dir)
        remote_root = (
            "\\\\" + self.server
            + "\\" + self.share
            + "\\" + self.root.replace("/", "\\")
        )
        return remote_root + "\\" + relative.replace(os.sep, "\\")
