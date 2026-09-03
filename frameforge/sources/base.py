"""What acquisition needs from any camera or frame producer."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np


class SourceDisconnect(Exception):
    pass


@dataclass(slots=True)
class Frame:
    array: np.ndarray
    ts_ns: int
    seq: int | None


class FrameSource(Protocol):
    def open(self) -> None: ...

    # Returns None for a frame the device reported as bad. Raises
    # SourceDisconnect when the device is gone. The returned array is only
    # valid until the next grab() or close().
    def grab(self) -> Frame | None: ...

    def close(self) -> None: ...
