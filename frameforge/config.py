"""Typed configuration: YAML base + environment overrides, validated at load.

Python 3.6-safe (Jetson JetPack): uses the `dataclasses` backport on <3.7.
Secrets (SMB credentials) are NOT part of this config — they're consumed by the
OS `mount -t cifs` step. The app only ever writes to the already-mounted
`paths.vast_dest`, so no password ever reaches Python.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


@dataclass
class CameraCfg:
    id: str                      # logical id → metadata_helper cam_XX
    serial: str                  # Basler serial; "" = first device found
    pfs: str = ""                # pylon feature-stream file applied on (re)connect


@dataclass
class EncodeCfg:
    backend: str = "nvv4l2h264enc"   # Jetson HW encoder; swap per platform
    bitrate_mbps: float = 1.0
    control: str = "vbr"
    fps: float = 50.0
    chunk_secs: float = 1800.0       # 30-min chunks
    gop: int = 250
    bframes: int = 0
    faststart: bool = True
    pix_fmt: str = "nv12"            # HW encoder input; gray not available on nvenc


@dataclass
class PathsCfg:
    scratch: str = "/var/lib/frameforge/scratch"
    vast_dest: str = "/mnt/vast/cdracos/frameforge_test"   # a TEST path, not prod tree


@dataclass
class Config:
    cameras: List[CameraCfg]
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    width: int = 1280
    height: int = 1024
    channels: int = 1
    ring_slots: int = 128
    queue_depth: int = 256
    metrics_port: int = 9100
    log_dir: str = "/var/log/frameforge"

    def validate(self) -> None:
        if not self.cameras:
            raise ValueError("config: at least one camera required")
        ids = [c.id for c in self.cameras]
        if len(ids) != len(set(ids)):
            raise ValueError("config: duplicate camera ids")
        e = self.encode
        if e.fps <= 0 or e.chunk_secs <= 0 or e.bitrate_mbps <= 0:
            raise ValueError("config: fps/chunk_secs/bitrate_mbps must be > 0")
        if self.ring_slots < 2 or self.queue_depth < 1:
            raise ValueError("config: ring_slots>=2 and queue_depth>=1 required")
        if self.channels not in (1, 3):
            raise ValueError("config: channels must be 1 or 3")
        if not self.paths.vast_dest:
            raise ValueError("config: paths.vast_dest required")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def load_config(path: Optional[str] = None) -> Config:
    """Load YAML (FF_CONFIG or arg), apply env overrides, validate, return Config."""
    path = path or _env("FF_CONFIG", "config/jetson.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    cameras = [CameraCfg(**c) for c in raw.get("cameras", [])]
    encode = EncodeCfg(**raw.get("encode", {}))
    paths = PathsCfg(**raw.get("paths", {}))

    cfg = Config(
        cameras=cameras,
        encode=encode,
        paths=paths,
        width=raw.get("width", 1280),
        height=raw.get("height", 1024),
        channels=raw.get("channels", 1),
        ring_slots=raw.get("ring", {}).get("slots", 128),
        queue_depth=raw.get("queue", {}).get("depth", 256),
        metrics_port=raw.get("metrics", {}).get("port", 9100),
        log_dir=raw.get("log_dir", "/var/log/frameforge"),
    )

    # Environment overrides (paths/ports — keep secrets out of YAML).
    if _env("FF_VAST_DEST"):
        cfg.paths.vast_dest = _env("FF_VAST_DEST")
    if _env("FF_SCRATCH"):
        cfg.paths.scratch = _env("FF_SCRATCH")
    if _env("FF_METRICS_PORT"):
        cfg.metrics_port = int(_env("FF_METRICS_PORT"))
    if _env("FF_LOG_DIR"):
        cfg.log_dir = _env("FF_LOG_DIR")

    cfg.validate()
    return cfg
