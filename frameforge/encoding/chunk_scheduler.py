"""Per-camera wall-clock chunk index + path."""

import datetime
import os
from zoneinfo import ZoneInfo


_SIDECAR_SUFFIX = ".h5"


def sidecar_for(mp4_path: str) -> str:
    return mp4_path[:-len(".mp4")] + _SIDECAR_SUFFIX


class ChunkScheduler:
    def __init__(self, *, scratch_dir: str, session_name: str,
                 camera_id: str, chunk_seconds: int,
                 timezone: str = "") -> None:
        self.scratch_dir = scratch_dir
        self.session_name = session_name
        self.camera_id = camera_id
        self.chunk_seconds = chunk_seconds
        self._tz = ZoneInfo(timezone) if timezone else None

    def current_chunk_index(self) -> int:
        now = self._now()
        elapsed_s = (now - self._midnight(now)).total_seconds()
        return max(0, int(elapsed_s // self.chunk_seconds))

    def chunk_path(self, chunk_index: int) -> str:
        return os.path.join(
            self.scratch_dir,
            self.session_name,
            self.camera_id,
            self._midnight(self._now()).strftime("%Y-%m-%d-%H-%M-%S"),
            "%s.%02d.mp4" % (self.camera_id, chunk_index),
        )

    def sidecar_path(self, chunk_index: int) -> str:
        return sidecar_for(self.chunk_path(chunk_index))

    def target_frames(self, fps: float) -> int:
        return int(round(self.chunk_seconds * fps))

    # Always aware: tz=None means system local, and a bare astimezone() would
    # convert a configured zone back to local.
    def _now(self) -> datetime.datetime:
        if self._tz is None:
            return datetime.datetime.now().astimezone()
        return datetime.datetime.now(self._tz)

    def _midnight(self, now: datetime.datetime) -> datetime.datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
