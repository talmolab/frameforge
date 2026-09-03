"""SMB share via smbprotocol: session, path mapping, atomic upload."""

import logging
import os
import shutil

import smbclient

from ..core.logging_setup import DEDUP_INTERVAL_S, DEDUP_KEY

_SMB_PORT = 445
_FAIL_LOG_INTERVAL_S = 600.0
_UPLOAD_BUFFER_BYTES = 4 * 1024 * 1024
_UPLOADING_SUFFIX = ".uploading"


class SmbStorage:
    def __init__(self, *, server: str, share: str, root: str) -> None:
        self.server = server
        self.share = share
        self.root = root
        self.location = f"//{server}/{share}/{root}"
        self.logger = logging.getLogger("frameforge.storage.smb")

        self._username = os.environ.get("SMB_USER")
        self._password = os.environ.get("SMB_PASS")
        if not self._username or not self._password:
            raise RuntimeError("storage.smb: SMB_USER/SMB_PASS env vars are required")

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
            self.logger.error(
                "SMB session registration failed err=%s", session_error,
                extra={DEDUP_KEY: "smb_register_fail",
                       DEDUP_INTERVAL_S: _FAIL_LOG_INTERVAL_S})
            return False

        self._alive = True
        self.logger.info("SMB session registered server=%s", self.server)
        return True

    def put(self, local_path: str, relative_path: str) -> None:
        remote_path = self._remote_path(relative_path)
        remote_dir = remote_path.rsplit("\\", 1)[0]
        uploading_path = remote_path + _UPLOADING_SUFFIX

        smbclient.makedirs(remote_dir, exist_ok=True)
        with open(local_path, "rb") as local_file:
            with smbclient.open_file(uploading_path, mode="wb") as remote_file:
                shutil.copyfileobj(
                    local_file, remote_file, length=_UPLOAD_BUFFER_BYTES)
        smbclient.replace(uploading_path, remote_path)

    def mark_dead(self) -> None:
        self._alive = False

    def close(self) -> None:
        try:
            smbclient.delete_session(self.server)
        except Exception:
            pass
        self.mark_dead()

    def _remote_path(self, relative_path: str) -> str:
        remote_root = (
            "\\\\" + self.server
            + "\\" + self.share
            + "\\" + self.root.replace("/", "\\")
        )
        return remote_root + "\\" + relative_path.replace("/", "\\").replace(os.sep, "\\")
