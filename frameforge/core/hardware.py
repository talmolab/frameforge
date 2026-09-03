"""Per-hardware-class specs: CPU pin map + broadcast capability.

Single source of truth for "what does this hardware support / how to use it."
Selected at startup via FF_HARDWARE env; unset means ``generic`` (no pinning,
no broadcast). Adding a new hardware class = add an enum variant, a
HardwareSpec, and (if needed) a pin function.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class HardwareClass(Enum):
    GENERIC = "generic"
    MS01 = "ms01"


PinFn = Callable[[str, int], set[int] | None]


@dataclass(frozen=True)
class HardwareSpec:
    name: str
    broadcast_enabled: bool
    broadcast_bitrate_mbps: float
    broadcast_codec_args: tuple[str, ...]
    pin_function: PinFn


def no_pin(worker_name: str, cam_index: int) -> set[int] | None:
    return None


_MS01_PAIRS_PER_CAM = 2
_MS01_E_CORE_BASE = 12
_MS01_TRANSFER_CPU = 18
_MS01_METRICS_CPU = 19


def _ms01_pin(worker_name: str, cam_index: int) -> set[int] | None:
    if worker_name.startswith("acq:"):
        return {_MS01_PAIRS_PER_CAM * cam_index}
    if worker_name.startswith("enc:"):
        return {_MS01_PAIRS_PER_CAM * cam_index + 1}
    if worker_name.startswith("bcast:"):
        return {_MS01_E_CORE_BASE + cam_index}
    if worker_name == "transfer":
        return {_MS01_TRANSFER_CPU}
    if worker_name in ("metrics", "host_sampler"):
        return {_MS01_METRICS_CPU}
    return None


_MS01_SPEC = HardwareSpec(
    name=HardwareClass.MS01.value,
    broadcast_enabled=True,
    broadcast_bitrate_mbps=1.0,
    broadcast_codec_args=(
        "-c:v", "h264_qsv",
        "-preset", "veryfast",
        "-profile:v", "baseline",
        "-look_ahead", "0",
        "-g", "20",
        "-bf", "0",
        "-pix_fmt", "nv12",
    ),
    pin_function=_ms01_pin,
)

_GENERIC_SPEC = HardwareSpec(
    name=HardwareClass.GENERIC.value,
    broadcast_enabled=False,
    broadcast_bitrate_mbps=1.0,
    broadcast_codec_args=(),
    pin_function=no_pin,
)

_SPECS: dict[str, HardwareSpec] = {
    HardwareClass.GENERIC.value: _GENERIC_SPEC,
    HardwareClass.MS01.value: _MS01_SPEC,
}


def get_hardware_spec(name: str) -> HardwareSpec:
    return _SPECS.get(name, _GENERIC_SPEC)
