"""Data returned by Node verbs, shared by the CLI, the management server and the hub.
Every type is a dataclass: ``asdict`` is its JSON, ``decode`` reads it back."""

import dataclasses


@dataclasses.dataclass
class DesiredState:
    """Operator intent that survives a restart; lives in desired.json, not tenant.yaml."""
    paused: bool = False
    session: str = ""
    maintenance: bool = False


@dataclasses.dataclass(frozen=True)
class ConfigState:
    """version_applied is what the running process loaded; restart_required is None
    until a process has recorded one."""
    version: int
    hash: str
    hand_edited: bool
    version_applied: int | None
    restart_required: bool | None


@dataclasses.dataclass(frozen=True)
class CameraState:
    """alive is None when the metrics endpoint is unreachable."""
    id: str
    serial: str
    alive: bool | None


@dataclasses.dataclass(frozen=True)
class DiscoveredCamera:
    """cam_id is the camera's id in cameras.yaml, None when unassigned."""
    serial: str
    model: str
    ip: str
    cam_id: str | None


@dataclasses.dataclass(frozen=True)
class StreamUrl:
    """kind is hls, webrtc or rtsp."""
    cam_id: str
    kind: str
    url: str


@dataclasses.dataclass(frozen=True)
class ConfigField:
    section: str
    name: str
    type: str
    default: object
    doc: str
    restart_required: bool = True


@dataclasses.dataclass(frozen=True)
class ConfigDocument:
    """tenant.yaml and cameras.yaml as one object; hand_edited means the files changed
    since the last recorded write."""
    version: int
    hash: str
    hand_edited: bool
    tenant: dict
    cameras: list


@dataclasses.dataclass(frozen=True)
class Status:
    hostname: str
    device_id: str
    frameforge_sha: str
    units: dict
    uptime_s: float | None
    desired: DesiredState
    config: ConfigState
    cameras: list


@dataclasses.dataclass(frozen=True)
class ActionResult:
    action: str
    ok: bool
    detail: str = ""


TYPES = {cls.__name__: cls for cls in (
    DesiredState, ConfigState, CameraState, DiscoveredCamera, StreamUrl, ConfigField,
    ConfigDocument, Status, ActionResult)}

_NESTED = {"Status": {"desired": "DesiredState", "config": "ConfigState",
                      "cameras": "list[CameraState]"}}


def decode(returns: str, data):
    """Build the dataclass named by a verb's ``returns`` from its JSON form."""
    if returns == "lines" or data is None:
        return data
    if returns.startswith("list[") and returns.endswith("]"):
        return [decode(returns[5:-1], item) for item in data]
    nested = _NESTED.get(returns, {})
    return TYPES[returns](**{
        key: decode(nested[key], value) if key in nested else value
        for key, value in data.items()})
