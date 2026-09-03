"""Per-camera wall-clock chunk index + path."""

import os
import time


_SIDECAR_SUFFIX = ".h5"
_DAY_FORMAT = "%Y-%m-%d-%H-%M-%S"


def sidecar_for(mp4_path: str) -> str:
    return mp4_path[:-len(".mp4")] + _SIDECAR_SUFFIX


class ChunkScheduler:
    def __init__(self, *, scratch_dir: str, session_name: str,
                 camera_id: str, chunk_seconds: int) -> None:
        self.scratch_dir = scratch_dir
        self.session_name = session_name
        self.camera_id = camera_id
        self.chunk_seconds = chunk_seconds

    def current_chunk_index(self) -> int:
        now = time.time()
        return max(0, int((now - _day_start(now)) // self.chunk_seconds))

    def chunk_path(self, chunk_index: int) -> str:
        day = time.strftime(_DAY_FORMAT, time.localtime(_day_start(time.time())))
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


# Local midnight as epoch seconds. mktime with isdst=-1 resolves DST itself, so
# elapsed time stays real across the 23h/25h days: indices 0-22 / 0-24.
def _day_start(now: float) -> float:
    local = time.localtime(now)
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
