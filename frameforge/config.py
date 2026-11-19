"""YAML config, validated at load.

Three layers, applied in order (later overrides earlier):
  1. Code defaults (dataclass field defaults)
  2. Hardware spec (FF_HARDWARE env -> core.hardware lookup; sets broadcast)
  3. Tenant YAML (/etc/frameforge/tenant.yaml; storage destination + overrides)

Per-rig cameras list lives in /etc/frameforge/cameras.yaml (separate concern).
Storage credentials live in env vars read by the storage backend.
"""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .core.hardware import HardwareClass, PinFn, get_hardware_spec, no_pin
from .core.paths import CAMERAS_FILE, TENANT_FILE
from .sources import validate_source
from .storage import validate_storage


def _doc(default, text: str):
    """A field with a one-line description, surfaced by `ff config_schema` and the hub."""
    return field(default=default, metadata={"doc": text})


@dataclass(slots=True)
class CameraCfg:
    id: str
    kind: str = "pylon"
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class AcqCfg:
    width: int = _doc(1280, "frame width, pixels")
    height: int = _doc(1024, "frame height, pixels")
    channels: int = _doc(1, "1 for mono, 3 for color")
    ring_slots: int = _doc(128, "frames buffered between camera and encoder")
    jumbo_frames: bool = _doc(False, "9000-byte packets; the switch must match")
    gige_subnet: str = _doc("192.168.10", "camera subnet; cam_0N is assigned .10N")


@dataclass(slots=True)
class EncodeCfg:
    fps: float = _doc(50.0, "frames per second")
    gop: int = _doc(250, "frames between keyframes; longer = smaller files, coarser seeking")
    crf: int = _doc(23, "quality; lower = better and larger")
    preset: str = _doc("superfast", "x264 speed/size tradeoff, ultrafast to slow")
    bframes: int = _doc(0, "bidirectional frames; 0 disables")
    denoise: bool = _doc(True, "temporal denoise before encoding")
    noise_reduction: int = _doc(0, "x264 nr strength; 0 disables")
    chunk_seconds: int = _doc(3600, "seconds of video per file")
    timezone: str = _doc("", "clock for chunk folder names; empty = box timezone")


@dataclass(slots=True)
class StorageCfg:
    kind: str = _doc("", "smb or s3")
    options: dict = field(default_factory=dict, metadata={"doc": "backend keys: server/share/root or bucket/prefix"})


@dataclass(slots=True)
class TransferCfg:
    storage: StorageCfg = field(default_factory=StorageCfg)
    low_disk_threshold_mb: int = _doc(500, "scratch free space that raises the low-disk alarm")
    analytics: bool = _doc(False, "log per-chunk size and frame stats on upload")


@dataclass(slots=True)
class BroadcastCfg:
    enabled: bool = False
    bitrate_mbps: float = 1.0
    codec_args: tuple[str, ...] = ()


@dataclass(slots=True)
class Config:
    cameras: list[CameraCfg]
    hardware: str = ""
    pin_function: PinFn = no_pin
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    acq: AcqCfg = field(default_factory=AcqCfg)
    transfer: TransferCfg = field(default_factory=TransferCfg)
    broadcast: BroadcastCfg = field(default_factory=BroadcastCfg)
    session_name: str = _doc("", "recording folder name; empty = start date")
    session_postfix: str = _doc("", "appended to a date-based session name")

    def validate(self) -> None:
        if not self.cameras:
            raise ValueError("config: at least one camera required")
        camera_ids = [camera.id for camera in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            raise ValueError("config: duplicate camera ids")
        for camera in self.cameras:
            validate_source(camera.kind, camera.options)

        if self.encode.fps <= 0:
            raise ValueError("config: encode.fps must be > 0")
        if self.encode.chunk_seconds <= 0:
            raise ValueError("config: encode.chunk_seconds must be > 0")
        if self.encode.bframes < 0:
            raise ValueError("config: encode.bframes must be >= 0")
        if self.encode.noise_reduction < 0:
            raise ValueError("config: encode.noise_reduction must be >= 0")
        if self.encode.timezone:
            try:
                ZoneInfo(self.encode.timezone)
            except ZoneInfoNotFoundError:
                raise ValueError(
                    f"config: encode.timezone {self.encode.timezone!r} unknown") from None
        if self.acq.ring_slots < 2:
            raise ValueError("config: acq.ring_slots>=2 required")
        if self.acq.channels not in (1, 3):
            raise ValueError("config: acq.channels must be 1 or 3")
        if self.broadcast.enabled and not self.broadcast.codec_args:
            raise ValueError(
                f"config: broadcast enabled but hardware {self.hardware!r} has no codec")

        validate_storage(self.transfer.storage.kind, self.transfer.storage.options)


def _env(name: str) -> str | None:
    return os.environ.get(name)


def _read_yaml(path: str) -> dict:
    with open(path) as raw_file:
        return yaml.safe_load(raw_file) or {}


def _read_tenant() -> dict:
    if not os.path.isfile(TENANT_FILE):
        raise ValueError(
            f"config: tenant file not found at {TENANT_FILE}")
    return _read_yaml(TENANT_FILE)


def _camera_from_raw(raw: dict) -> CameraCfg:
    options = dict(raw)
    return CameraCfg(id=options.pop("id"), kind=options.pop("kind", "pylon"),
                     options=options)


def _storage_from_raw(raw: dict) -> StorageCfg:
    options = dict(raw)
    return StorageCfg(kind=options.pop("kind", ""), options=options)


def _load_cameras() -> list[CameraCfg]:
    if not os.path.isfile(CAMERAS_FILE):
        raise ValueError(
            f"config: cameras file not found at {CAMERAS_FILE}")
    raw_cameras = _read_yaml(CAMERAS_FILE).get("cameras", [])
    return [_camera_from_raw(camera) for camera in raw_cameras]


def _build(tenant: dict, cameras: list[CameraCfg], hardware_name: str) -> Config:
    spec = get_hardware_spec(hardware_name)

    broadcast_raw = tenant.get("broadcast", {})
    transfer_raw = dict(tenant.get("transfer", {}))
    storage_raw = transfer_raw.pop("storage", {})

    return Config(
        cameras=cameras,
        hardware=hardware_name,
        pin_function=spec.pin_function,
        encode=EncodeCfg(**tenant.get("encode", {})),
        acq=AcqCfg(**tenant.get("acq", {})),
        transfer=TransferCfg(storage=_storage_from_raw(storage_raw), **transfer_raw),
        broadcast=BroadcastCfg(
            enabled=broadcast_raw.get("enabled", spec.broadcast_enabled),
            bitrate_mbps=broadcast_raw.get("bitrate_mbps", spec.broadcast_bitrate_mbps),
            codec_args=spec.broadcast_codec_args),
        session_name=tenant.get("session_name", ""),
        session_postfix=tenant.get("session_postfix", ""),
    )


def hardware_name() -> str:
    return _env("FF_HARDWARE") or HardwareClass.GENERIC.value


def build_config(tenant: dict, cameras: list[dict], hardware: str | None = None) -> Config:
    """Config from raw YAML structures. The entrypoint validates posted documents through it."""
    return _build(tenant, [_camera_from_raw(camera) for camera in cameras],
                  hardware or hardware_name())


def load_config() -> Config:
    config = _build(_read_tenant(), _load_cameras(), hardware_name())
    config.validate()
    return config


# Tenant-only view for tools that run outside the pipeline (heartbeat):
# no cameras file, no FF_HARDWARE.
def load_tenant_config() -> Config:
    config = _build(_read_tenant(), [], HardwareClass.GENERIC.value)
    validate_storage(config.transfer.storage.kind, config.transfer.storage.options)
    return config
