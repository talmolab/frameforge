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

import yaml

from .core.hardware import HardwareClass, PinFn, get_hardware_spec, no_pin
from .core.paths import CAMERAS_FILE, TENANT_FILE
from .sources import validate_source
from .storage import validate_storage


@dataclass(slots=True)
class CameraCfg:
    id: str
    kind: str = "pylon"
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class AcqCfg:
    width: int = 1280
    height: int = 1024
    channels: int = 1
    jumbo_frames: bool = False
    gige_subnet: str = "192.168.10"


@dataclass(slots=True)
class EncodeCfg:
    fps: float = 50.0
    chunk_seconds: int = 3600


@dataclass(slots=True)
class StorageCfg:
    kind: str = ""
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class TransferCfg:
    storage: StorageCfg = field(default_factory=StorageCfg)
    analytics: bool = False


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
    session_name: str = ""

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
            bitrate_mbps=spec.broadcast_bitrate_mbps,
            codec_args=spec.broadcast_codec_args),
        session_name=tenant.get("session_name", ""),
    )


def load_config() -> Config:
    hardware_name = _env("FF_HARDWARE") or HardwareClass.GENERIC.value

    config = _build(_read_tenant(), _load_cameras(), hardware_name)
    config.validate()
    return config


# Tenant-only view for tools that run outside the pipeline (heartbeat):
# no cameras file, no FF_HARDWARE.
def load_tenant_config() -> Config:
    config = _build(_read_tenant(), [], HardwareClass.GENERIC.value)
    validate_storage(config.transfer.storage.kind, config.transfer.storage.options)
    return config
