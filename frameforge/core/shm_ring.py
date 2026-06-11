"""Shared-memory frame ring for zero-copy hand-off between processes.

Python 3.6-safe: uses ``multiprocessing.RawArray`` (not 3.8+ ``shared_memory``).

- A fixed pool of frame-sized slots lives in one shared buffer.
- A free-list queue tracks which slots are available; it's the back-pressure
  point — when the encoder is behind, acquisition blocks on ``get_free`` and
  never drops in code (drops would happen at pylon's buffer if blocked too
  long; tracked via an ``incomplete`` counter there).
- Hand-off passes only a slot index across the process boundary; the bytes
  stay in shared memory.
"""

import ctypes
import multiprocessing
from typing import Tuple

import numpy as np


class FrameRing:
    def __init__(self, slots: int, height: int, width: int,
                 channels: int = 1) -> None:
        if slots < 2:
            raise ValueError("ring needs >= 2 slots")
        self.slots = slots
        self.height = height
        self.width = width
        self.channels = channels
        self.slot_bytes = height * width * channels
        self._buffer = multiprocessing.RawArray(
            ctypes.c_uint8, slots * self.slot_bytes)
        self._free_indices = multiprocessing.Queue(maxsize=slots)
        for index in range(slots):
            self._free_indices.put(index)

    @property
    def shape(self) -> Tuple[int, ...]:
        if self.channels == 1:
            return (self.height, self.width)
        return (self.height, self.width, self.channels)

    def get_free(self, timeout=None) -> int:
        return self._free_indices.get(timeout=timeout)

    def release(self, slot_index: int) -> None:
        self._free_indices.put(slot_index)

    def view(self, slot_index: int) -> np.ndarray:
        offset = slot_index * self.slot_bytes
        flat = np.frombuffer(self._buffer, dtype=np.uint8,
                             count=self.slot_bytes, offset=offset)
        return flat.reshape(self.shape)

    def free_count(self) -> int:
        try:
            return self._free_indices.qsize()
        except NotImplementedError:
            return -1
