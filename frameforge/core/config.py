"""YAML config + env overrides, validated at load.

Profile selection: ``FF_PROFILE=bench|prod`` picks ``config/<profile>.yaml``.
Secrets (SMB credentials) live in env vars read by Transfer, not in YAML.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


@dataclass
class CameraCfg:
    id: str
    serial: str = ""
    pfs: str = ""


@dataclass
class EncodeCfg:
    backend: str = "libx264"
    bitrate_mbps: float = 1.0
    fps: float = 50.0
    gop: int = 250
    crf: int = 21
    preset: str = "superfast"
    chunk_seconds: int = 3600


@dataclass
class AcqCfg:
    packet_size: int = 1500
    inter_packet_delay_ns: int = 0
    max_num_buffer: int = 100
    retrieve_timeout_ms: int = 0


@dataclass
class TransferCfg:
    smb_server: str = "pool1.vast.salk.edu"
    smb_share: str = "talmo"
    smb_root: str = "cdracos/frameforge_test"
    scan_interval_s: float = 30.0
    low_disk_threshold_mb: int = 500
    analytics: bool = False


@dataclass
class BroadcastCfg:
    enabled: bool = True
    backend: str = "libx264"
    crf: int = 23
    preset: str = "superfast"
    rtsp_host: str = "127.0.0.1"
    rtsp_port: int = 8554


@dataclass
class Config:
    cameras: List[CameraCfg]
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    acq: AcqCfg = field(default_factory=AcqCfg)
    transfer: TransferCfg = field(default_factory=TransferCfg)
    broadcast: BroadcastCfg = field(default_factory=BroadcastCfg)
    scratch_dir: str = "/var/lib/frameforge/scratch"
    session_name: str = ""
    width: int = 1280
    height: int = 1024
    channels: int = 1
    ring_slots: int = 128
    queue_depth: int = 256
    log_dir: str = "/var/log/frameforge"

    def validate(self) -> None:
        if not self.cameras:
            raise ValueError("config: at least one camera required")
        camera_ids = [camera.id for camera in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            raise ValueError("config: duplicate camera ids")

        encode = self.encode
        if encode.fps <= 0:
            raise ValueError("config: encode.fps must be > 0")
        if encode.backend not in ("libx264", "nvv4l2h265enc"):
            raise ValueError(
                "config: encode.backend must be 'libx264' or 'nvv4l2h265enc'")
        if encode.chunk_seconds <= 0:
            raise ValueError("config: encode.chunk_seconds must be > 0")
        if self.ring_slots < 2 or self.queue_depth < 1:
            raise ValueError("config: ring_slots>=2 and queue_depth>=1 required")
        if self.channels not in (1, 3):
            raise ValueError("config: channels must be 1 or 3")

        transfer = self.transfer
        if transfer.scan_interval_s <= 0:
            raise ValueError("config: transfer.scan_interval_s>0 required")
        if not transfer.smb_server or not transfer.smb_share:
            raise ValueError("config: transfer.smb_server/smb_share required")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def load_config(path: Optional[str] = None) -> Config:
    if path is None:
        profile = _env("FF_PROFILE")
        if not profile:
            raise ValueError("config: set FF_PROFILE=bench|prod")
        path = os.path.join("config", profile + ".yaml")

    with open(path) as raw_file:
        raw = yaml.safe_load(raw_file) or {}

    cameras = [CameraCfg(**camera) for camera in raw.get("cameras", [])]
    config = Config(
        cameras=cameras,
        encode=EncodeCfg(**raw.get("encode", {})),
        acq=AcqCfg(**raw.get("acq", {})),
        transfer=TransferCfg(**raw.get("transfer", {})),
        broadcast=BroadcastCfg(**raw.get("broadcast", {})),
        scratch_dir=raw.get("scratch_dir", "/var/lib/frameforge/scratch"),
        session_name=raw.get("session_name", ""),
        width=raw.get("width", 1280),
        height=raw.get("height", 1024),
        channels=raw.get("channels", 1),
        ring_slots=raw.get("ring", {}).get("slots", 128),
        queue_depth=raw.get("queue", {}).get("depth", 256),
        log_dir=raw.get("log_dir", "/var/log/frameforge"),
    )

    if _env("FF_SCRATCH"):     config.scratch_dir         = _env("FF_SCRATCH")
    if _env("FF_LOG_DIR"):     config.log_dir             = _env("FF_LOG_DIR")
    if _env("FF_VAST_SERVER"): config.transfer.smb_server = _env("FF_VAST_SERVER")
    if _env("FF_VAST_SHARE"):  config.transfer.smb_share  = _env("FF_VAST_SHARE")
    if _env("FF_VAST_ROOT"):   config.transfer.smb_root   = _env("FF_VAST_ROOT")

    config.validate()
    return config
