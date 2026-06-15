"""Per-camera wall-clock chunk index + path."""

import datetime
import os
from zoneinfo import ZoneInfo


_TZ = ZoneInfo("America/Los_Angeles")


class ChunkScheduler:
    def __init__(self, *, scratch_dir: str, session_name: str,
                 camera_id: str, chunk_seconds: int) -> None:
        self.scratch_dir = scratch_dir
        self.session_name = session_name
        self.camera_id = camera_id
        self.chunk_seconds = chunk_seconds

    def current_chunk_index(self) -> int:
        elapsed_s = (datetime.datetime.now().astimezone()
                     - self._today_midnight_aware()).total_seconds()
        return max(0, int(elapsed_s // self.chunk_seconds))

    def chunk_path(self, chunk_index: int) -> str:
        return os.path.join(
            self.scratch_dir,
            self.session_name,
            self.camera_id,
            self._today_midnight_str(),
            "%s.%02d.mp4" % (self.camera_id, chunk_index),
        )

    def target_frames(self, fps: float) -> int:
        return int(round(self.chunk_seconds * fps))

    # `_aware` form supplies the explicit-LA-TZ datetime anchor for
    # elapsed-seconds math; `_str` form is the recording_start directory
    # name. Kept separate so chunk_index math uses real datetimes.
    def _today_midnight_aware(self):
        now = datetime.datetime.now(_TZ)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _today_midnight_str(self) -> str:
        return self._today_midnight_aware().strftime("%Y-%m-%d-%H-%M-%S")
