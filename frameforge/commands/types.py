"""Data returned by Node verbs. Shared by the CLI and the management server."""

import dataclasses


@dataclasses.dataclass
class DesiredState:
    """Operator intent that must survive a restart. Lives in desired.json, not tenant.yaml."""
    paused: bool = False
    session: str = ""
    maintenance: bool = False


@dataclasses.dataclass(frozen=True)
class CameraState:
    id: str
    serial: str
    alive: bool | None


@dataclasses.dataclass(frozen=True)
class DiscoveredCamera:
    serial: str
    model: str
    ip: str
    slot: str | None


@dataclasses.dataclass(frozen=True)
class Status:
    hostname: str
    units: dict[str, str]
    desired: DesiredState
    config_hash: str
    uptime_s: float | None
    cameras: list[CameraState]


@dataclasses.dataclass(frozen=True)
class ActionResult:
    action: str
    ok: bool
    detail: str = ""
