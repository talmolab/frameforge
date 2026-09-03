"""What acquisition needs from any camera, plus the registry of backends.
Backends import lazily so the package loads without their SDKs."""

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


_OPTIONS = {
    "pylon": {"serial", "pfs"},
}

SOURCE_KINDS = tuple(_OPTIONS)


def validate_source(kind: str, options: dict) -> None:
    if kind not in _OPTIONS:
        raise ValueError(f"config: camera kind {kind!r} not in {SOURCE_KINDS}")
    unknown = set(options) - _OPTIONS[kind]
    if unknown:
        raise ValueError(f"config: camera kind {kind!r} does not take {sorted(unknown)}")


def make_source(camera_config, acq, fps, **extra) -> FrameSource:
    if camera_config.kind == "pylon":
        from .pylon import PylonSource
        return PylonSource(camera_config.id, acq, fps,
                           **camera_config.options, **extra)
    raise ValueError(f"unknown source kind {camera_config.kind!r}")
