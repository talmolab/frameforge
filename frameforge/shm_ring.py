"""Shared-memory frame ring for zero-copy hand-off between processes.

Python 3.6-safe: uses ``multiprocessing.RawArray`` (not 3.8+ ``shared_memory``).
A fixed pool of frame-sized slots lives in one shared buffer; a free-list queue
tracks which slots are available.

Flow (acquisition → encoder), with the free-list as the back-pressure point:
    idx = ring.get_free()              # blocks when encoder is behind → NO drop
    np.copyto(ring.view(idx), frame)   # one memcopy out of the camera buffer
    data_q.put((idx, hw_ts, host_ts))  # hand a *handle* across the process boundary
    ...encoder...
    idx, hw_ts, host_ts = data_q.get()
    encode(ring.view(idx))             # same physical bytes, no extra copy
    ring.release(idx)
"""
from __future__ import annotations

import ctypes
import multiprocessing as mp
from typing import Tuple

import numpy as np


class FrameRing:
    def __init__(self, slots: int, height: int, width: int, channels: int = 1):
        if slots < 2:
            raise ValueError("ring needs >= 2 slots")
        self.slots = slots
        self.height = height
        self.width = width
        self.channels = channels
        self.itemsize = height * width * channels
        self._buf = mp.RawArray(ctypes.c_uint8, slots * self.itemsize)
        self._free = mp.Queue(maxsize=slots)
        for i in range(slots):
            self._free.put(i)

    @property
    def shape(self) -> Tuple[int, ...]:
        if self.channels == 1:
            return (self.height, self.width)
        return (self.height, self.width, self.channels)

    def get_free(self, timeout=None) -> int:
        """Pop a free slot index; blocks (back-pressure) until one is available."""
        return self._free.get(timeout=timeout)

    def release(self, idx: int) -> None:
        self._free.put(idx)

    def view(self, idx: int) -> np.ndarray:
        """numpy view onto slot `idx` — no copy; backed by the shared buffer."""
        off = idx * self.itemsize
        flat = np.frombuffer(self._buf, dtype=np.uint8, count=self.itemsize, offset=off)
        return flat.reshape(self.shape)

    def free_count(self) -> int:
        try:
            return self._free.qsize()         # not implemented on macOS; best-effort
        except NotImplementedError:
            return -1
