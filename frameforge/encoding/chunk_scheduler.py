"""Per-camera wall-clock chunk index + path."""

import datetime
import os
import time
from zoneinfo import ZoneInfo


_SIDECAR_SUFFIX = ".h5"
_DAY_FORMAT = "%Y-%m-%d-%H-%M-%S"


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
        now = time.time()
        return max(0, int((now - self._day_start(now)) // self.chunk_seconds))

    def chunk_path(self, chunk_index: int) -> str:
        day_start = self._day_start(time.time())
        day = datetime.datetime.fromtimestamp(day_start, self._tz).strftime(_DAY_FORMAT)
        return os.path.join(
            self.scratch_dir,
            self.session_name,
            self.camera_id,
            day,
            "%s.%02d.mp4" % (self.camera_id, chunk_index),
        )

    def sidecar_path(self, chunk_index: int) -> str:
        return sidecar_for(self.chunk_path(chunk_index))

    def target_frames(self, fps: float) -> int:
        return int(round(self.chunk_seconds * fps))

    # Midnight as epoch seconds, so elapsed time stays real across DST and
    # hourly indices run 0-22 / 0-24 on the 23h/25h days. tz=None -> system
    # local via mktime (isdst=-1 lets libc resolve the offset).
    def _day_start(self, now: float) -> float:
        if self._tz is None:
            local = time.localtime(now)
            return time.mktime(
                (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        midnight = datetime.datetime.fromtimestamp(now, self._tz).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
