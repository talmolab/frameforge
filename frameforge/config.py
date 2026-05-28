"""YAML config + env overrides, validated at load.

- Python 3.6-safe (Jetson JetPack) via the ``dataclasses`` backport.
- No silent default profile: caller must set ``FF_PROFILE=bench|prod`` (the YAML
  ``config/<profile>.yaml`` is loaded) or ``FF_CONFIG=<path>`` for an explicit file.
- Code defaults are neutral library minimums; deployment-specific values live
  in the chosen YAML.
- Secrets (SMB credentials) are NOT here; they're read by Transfer from env.
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
    backend: str = "nvv4l2h265enc"
    bitrate_mbps: float = 1.0
    fps: float = 50.0
    gop: int = 250


@dataclass
class AcqCfg:
    packet_size: int = 1500
    inter_packet_delay_ns: int = 0
    max_num_buffer: int = 64
    retrieve_timeout_ms: int = 0


@dataclass
class PathsCfg:
    scratch: str = "/var/lib/frameforge/scratch"


@dataclass
class TransferCfg:
    smb_server: str = "pool1.vast.salk.edu"
    smb_share: str = "talmo"
    smb_root: str = "cdracos/frameforge_test"
    scan_interval_s: float = 30.0
    max_attempts_per_chunk: int = 30
    low_disk_threshold_mb: int = 500


@dataclass
class RecordingCfg:
    # Must satisfy metadata_helper: ^\d{4}-\d{2}-\d{2}-\w+$
    session_name: str = ""


@dataclass
class Config:
    cameras: List[CameraCfg]
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    acq: AcqCfg = field(default_factory=AcqCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    transfer: TransferCfg = field(default_factory=TransferCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
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
        if encode.fps <= 0 or encode.bitrate_mbps <= 0:
            raise ValueError("config: fps/bitrate_mbps must be > 0")
        if self.ring_slots < 2 or self.queue_depth < 1:
            raise ValueError("config: ring_slots>=2 and queue_depth>=1 required")
        if self.channels not in (1, 3):
            raise ValueError("config: channels must be 1 or 3")

        transfer = self.transfer
        if not transfer.smb_server or not transfer.smb_share:
            raise ValueError("config: transfer.smb_server/smb_share required")
        if transfer.scan_interval_s <= 0 or transfer.max_attempts_per_chunk < 1:
            raise ValueError("config: transfer.scan_interval_s>0 and max_attempts_per_chunk>=1")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def _resolve_config_path() -> str:
    explicit_path = _env("FF_CONFIG")
    if explicit_path:
        return explicit_path

    profile = _env("FF_PROFILE")
    if profile:
        return os.path.join("config", profile + ".yaml")

    raise ValueError(
        "config: set FF_PROFILE=bench|prod or FF_CONFIG=<path>; neither was set")


def load_config(path: Optional[str] = None) -> Config:
    path = path or _resolve_config_path()
    with open(path) as raw_file:
        raw = yaml.safe_load(raw_file) or {}

    cameras = [CameraCfg(**camera) for camera in raw.get("cameras", [])]
    config = Config(
        cameras=cameras,
        encode=EncodeCfg(**raw.get("encode", {})),
        acq=AcqCfg(**raw.get("acq", {})),
        paths=PathsCfg(**raw.get("paths", {})),
        transfer=TransferCfg(**raw.get("transfer", {})),
        recording=RecordingCfg(**raw.get("recording", {})),
        width=raw.get("width", 1280),
        height=raw.get("height", 1024),
        channels=raw.get("channels", 1),
        ring_slots=raw.get("ring", {}).get("slots", 128),
        queue_depth=raw.get("queue", {}).get("depth", 256),
        log_dir=raw.get("log_dir", "/var/log/frameforge"),
    )

    if _env("FF_SCRATCH"):     config.paths.scratch       = _env("FF_SCRATCH")
    if _env("FF_LOG_DIR"):     config.log_dir             = _env("FF_LOG_DIR")
    if _env("FF_VAST_SERVER"): config.transfer.smb_server = _env("FF_VAST_SERVER")
    if _env("FF_VAST_SHARE"):  config.transfer.smb_share  = _env("FF_VAST_SHARE")
    if _env("FF_VAST_ROOT"):   config.transfer.smb_root   = _env("FF_VAST_ROOT")

    config.validate()
    return config
