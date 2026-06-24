"""Per-hardware-class specs: CPU pin map + broadcast capability.

Single source of truth for "what does this hardware support / how to use it."
Selected at startup via FF_HARDWARE env. Adding a new hardware class = add an
enum variant, a HardwareSpec, and (if needed) a pin function.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class HardwareClass(Enum):
    MS01 = "ms01"


PinFn = Callable[[str, int], set[int] | None]


@dataclass(frozen=True)
class HardwareSpec:
    name: str
    broadcast_enabled: bool
    broadcast_bitrate_mbps: float
    pin_function: PinFn


def _no_pin(worker_name: str, cam_index: int) -> set[int] | None:
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
    pin_function=_ms01_pin,
)

_UNKNOWN_SPEC = HardwareSpec(
    name="unknown",
    broadcast_enabled=False,
    broadcast_bitrate_mbps=1.0,
    pin_function=_no_pin,
)

_SPECS: dict[str, HardwareSpec] = {
    HardwareClass.MS01.value: _MS01_SPEC,
}


def get_hardware_spec(name: str) -> HardwareSpec:
    return _SPECS.get(name, _UNKNOWN_SPEC)
