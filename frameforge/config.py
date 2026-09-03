"""YAML config + env overrides, validated at load.

Three layers, applied in order (later overrides earlier):
  1. Code defaults (dataclass field defaults)
  2. Hardware spec (FF_HARDWARE env -> core.hardware lookup; sets broadcast)
  3. Tenant YAML (/etc/frameforge/tenant.yaml; per-lab SMB dest + any overrides)

Per-rig cameras list lives in /etc/frameforge/cameras.yaml (separate concern).
Secrets (VAST_USER/VAST_PASS) live in env vars read by SmbSession.
"""

import os
from dataclasses import dataclass, field

import yaml

from .core.hardware import get_hardware_spec
from .core.paths import CAMERAS_FILE, TENANT_FILE
from .sources import SOURCE_KINDS


@dataclass(slots=True)
class CameraCfg:
    id: str
    kind: str = "pylon"
    serial: str = ""
    pfs: str = ""


@dataclass(slots=True)
class AcqCfg:
    width: int = 1280
    height: int = 1024
    channels: int = 1
    ring_slots: int = 128
    jumbo_frames: bool = False
    gige_subnet: str = "192.168.10"


@dataclass(slots=True)
class EncodeCfg:
    fps: float = 50.0
    gop: int = 250
    crf: int = 21
    preset: str = "superfast"
    chunk_seconds: int = 3600


@dataclass(slots=True)
class TransferCfg:
    smb_server: str = ""
    smb_share: str = ""
    smb_root: str = ""
    low_disk_threshold_mb: int = 500
    analytics: bool = False


@dataclass(slots=True)
class BroadcastCfg:
    enabled: bool = False
    bitrate_mbps: float = 1.0


@dataclass(slots=True)
class Config:
    cameras: list[CameraCfg]
    hardware: str = ""
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    acq: AcqCfg = field(default_factory=AcqCfg)
    transfer: TransferCfg = field(default_factory=TransferCfg)
    broadcast: BroadcastCfg = field(default_factory=BroadcastCfg)
    session_name: str = ""
    session_postfix: str = ""

    def validate(self) -> None:
        if not self.cameras:
            raise ValueError("config: at least one camera required")
        camera_ids = [camera.id for camera in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            raise ValueError("config: duplicate camera ids")
        for camera in self.cameras:
            if camera.kind not in SOURCE_KINDS:
                raise ValueError(
                    f"config: camera {camera.id} kind {camera.kind!r} "
                    f"not in {SOURCE_KINDS}")

        encode = self.encode
        if encode.fps <= 0:
            raise ValueError("config: encode.fps must be > 0")
        if encode.chunk_seconds <= 0:
            raise ValueError("config: encode.chunk_seconds must be > 0")
        if self.acq.ring_slots < 2:
            raise ValueError("config: acq.ring_slots>=2 required")
        if self.acq.channels not in (1, 3):
            raise ValueError("config: acq.channels must be 1 or 3")

        transfer = self.transfer
        if not transfer.smb_server or not transfer.smb_share or not transfer.smb_root:
            raise ValueError(
                "config: tenant must specify smb_server, smb_share, smb_root")


_TOP_LEVEL_FIELDS = ("session_name", "session_postfix")


def _env(name: str) -> str | None:
    return os.environ.get(name)


def _read_yaml(path: str) -> dict:
    with open(path) as raw_file:
        return yaml.safe_load(raw_file) or {}


def _load_cameras() -> list[CameraCfg]:
    if not os.path.isfile(CAMERAS_FILE):
        raise ValueError(
            f"config: cameras file not found at {CAMERAS_FILE}")
    raw_cameras = _read_yaml(CAMERAS_FILE).get("cameras", [])
    return [CameraCfg(**camera) for camera in raw_cameras]


def load_config() -> Config:
    hardware_name = _env("FF_HARDWARE")
    if not hardware_name:
        raise ValueError("config: set FF_HARDWARE=ms01")
    spec = get_hardware_spec(hardware_name)

    if not os.path.isfile(TENANT_FILE):
        raise ValueError(
            f"config: tenant file not found at {TENANT_FILE}")
    tenant = _read_yaml(TENANT_FILE)

    cameras = _load_cameras()

    broadcast_raw = {
        "enabled": spec.broadcast_enabled,
        "bitrate_mbps": spec.broadcast_bitrate_mbps,
    }
    broadcast_raw.update(tenant.get("broadcast", {}))

    config = Config(
        cameras=cameras,
        hardware=hardware_name,
        encode=EncodeCfg(**tenant.get("encode", {})),
        acq=AcqCfg(**tenant.get("acq", {})),
        transfer=TransferCfg(**tenant.get("transfer", {})),
        broadcast=BroadcastCfg(**broadcast_raw),
        **{k: tenant[k] for k in _TOP_LEVEL_FIELDS if k in tenant},
    )

    config.validate()
    return config
