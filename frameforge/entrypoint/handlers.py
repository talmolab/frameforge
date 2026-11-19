"""The verb implementations, grouped: Monitor reads, Control drives systemd,
Configure owns the config document. Node mixes the three in; each surface calls
``getattr(node, verb.name)``."""

import dataclasses
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator

from ..core import session
from ..core.paths import DESIRED_FILE, HOME_DIR
from . import document as store
from .setup import SECRETS_FILE, read_cameras
from .system import SystemAdapter
from .types import (ActionResult, CameraState, ConfigDocument, ConfigField, DesiredState,
                    DiscoveredCamera, Session, Status, StreamUrl)
from .verbs import Arg, verb

UNIT = "frameforge"
UNITS = ("frameforge", "heartbeat.timer", "mediamtx")
STREAM_PORTS = {"hls": ("http", 8888), "webrtc": ("http", 8889), "rtsp": ("rtsp", 8554)}
_METRICS_URL = "http://127.0.0.1:9100/metrics"
_METRICS_TIMEOUT_S = 1.0
_WANTED_METRICS = ("acq_camera_alive", "frameforge_uptime_seconds")


class Monitor:
    system: SystemAdapter

    @verb("units, cameras, config versions, desired state", returns="Status", mutates=False)
    def status(self) -> Status:
        metrics = _scrape_metrics()
        alive = metrics.get("acq_camera_alive", {})
        return Status(
            hostname=socket.gethostname(),
            device_id=secret("FF_DEVICE_ID"),
            frameforge_sha=git_sha(),
            units={unit: self.system.is_active(unit) for unit in UNITS},
            uptime_s=metrics.get("frameforge_uptime_seconds", {}).get(""),
            session=read_session(),
            desired=read_desired(),
            config=store.state(),
            cameras=[CameraState(id=cam["id"], serial=str(cam.get("serial", "")),
                                 alive=None if not metrics else alive.get(cam["id"], 0.0) >= 1)
                     for cam in read_cameras()],
        )

    @verb("cameras on the switch vs cameras.yaml", returns="list[DiscoveredCamera]", mutates=False)
    def cams(self) -> list[DiscoveredCamera]:
        from pypylon import pylon

        ids = {str(cam.get("serial", "")): cam["id"] for cam in read_cameras()}
        found = []
        for device in pylon.TlFactory.GetInstance().EnumerateDevices():
            serial = device.GetSerialNumber()
            found.append(DiscoveredCamera(
                serial=serial, model=device.GetModelName(),
                ip=device.GetIpAddress() if device.IsIpAddressAvailable() else "",
                cam_id=ids.get(serial)))
        return sorted(found, key=lambda cam: (cam.cam_id or "~", cam.serial))

    @verb("live stream URLs per camera", returns="list[StreamUrl]", mutates=False)
    def streams(self) -> list[StreamUrl]:
        host = socket.gethostname()
        return [StreamUrl(cam["id"], kind, f"{scheme}://{host}:{port}/{cam['id']}")
                for cam in read_cameras()
                for kind, (scheme, port) in STREAM_PORTS.items()]

    @verb("journal for the frameforge unit", Arg("n", "lines of history", int, 50),
          Arg("f", "follow", flag=True), returns="lines", mutates=False, streams=True)
    def logs(self, n: int = 50, f: bool = False) -> Iterator[str]:
        return self.system.journal(UNIT, n, f)


class Control:
    system: SystemAdapter

    @verb("start recording")
    def start(self) -> ActionResult:
        return _result("start", self.system.start(UNIT))

    @verb("finish the current chunk, then stop")
    def stop(self) -> ActionResult:
        return _result("stop", self.system.stop(UNIT))

    @verb("finish the current chunk, restart with config on disk")
    def restart(self) -> ActionResult:
        return _result("restart", self.system.restart(UNIT))

    @verb("SIGKILL now; current chunk discarded")
    def kill(self) -> ActionResult:
        return _result("kill", self.system.kill(UNIT, "SIGKILL"))

    @verb("stop and remember that the stop was intentional")
    def pause(self) -> ActionResult:
        write_desired(paused=True)
        return _result("pause", self.system.stop(UNIT))

    @verb("clear pause and start")
    def resume(self) -> ActionResult:
        write_desired(paused=False)
        return _result("resume", self.system.start(UNIT))

    @verb("pull and reinstall; restarts if running", Arg("ref", "git ref", default="main"))
    def update(self, ref: str = "main") -> ActionResult:
        installer = os.path.join(HOME_DIR, "deploy", "scripts", "install-frameforge.sh")
        if not os.path.isfile(installer):
            return ActionResult("update", False, f"installer not found at {installer}")
        if os.geteuid() != 0:
            return ActionResult("update", False, "update must run as root")

        with tempfile.TemporaryDirectory() as scratch:
            copy = shutil.copy(installer, scratch)
            result = subprocess.run(
                ["bash", copy], env={**os.environ, "GIT_REF": ref, "FF_HOME": HOME_DIR},
                capture_output=True, text=True)
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
        return ActionResult("update", result.returncode == 0, tail)


class Configure:
    @verb("tenant + cameras as one document, with version and hash",
          returns="ConfigDocument", mutates=False)
    def config(self) -> ConfigDocument:
        return store.read()

    @verb("every config field with type, default and description",
          returns="list[ConfigField]", mutates=False)
    def config_schema(self) -> list[ConfigField]:
        return store.schema()

    @verb("replace the whole config document; validated before any write",
          Arg("document", "ConfigDocument-shaped JSON: tenant + cameras", dict, body=True),
          Arg("if_version", "write only if the on-disk version still matches", int),
          cli=False)
    def config_put(self, document: dict, if_version: int | None = None) -> ActionResult:
        return store.write(document.get("tenant", {}), document.get("cameras", []), if_version)


def read_session() -> Session:
    current = session.read()
    if current is None:
        return Session(None, None, None)
    return Session(current["name"], current["started"], time.time() - current["started"])


def read_desired() -> DesiredState:
    try:
        with open(DESIRED_FILE) as raw:
            return DesiredState(**json.load(raw))
    except (OSError, ValueError, TypeError):
        return DesiredState()


def write_desired(**changes) -> None:
    desired = dataclasses.replace(read_desired(), **changes)
    os.makedirs(os.path.dirname(DESIRED_FILE), exist_ok=True)
    tmp_path = DESIRED_FILE + ".tmp"
    with open(tmp_path, "w") as raw:
        json.dump(dataclasses.asdict(desired), raw, indent=1)
        raw.write("\n")
    os.replace(tmp_path, DESIRED_FILE)


def _result(name: str, outcome: tuple[int, str]) -> ActionResult:
    returncode, output = outcome
    return ActionResult(name, returncode == 0, output)


def secret(key: str) -> str:
    try:
        with open(SECRETS_FILE) as raw:
            for line in raw:
                name, _, value = line.strip().partition("=")
                if name == key:
                    return value
    except OSError:
        pass
    return ""


def git_sha() -> str:
    result = subprocess.run(["git", "-C", HOME_DIR, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _scrape_metrics() -> dict[str, dict[str, float]]:
    try:
        with urllib.request.urlopen(_METRICS_URL, timeout=_METRICS_TIMEOUT_S) as response:
            text = response.read().decode()
    except (OSError, ValueError):
        return {}

    parsed: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name_part, _, value = line.rpartition(" ")
        name, _, labels = name_part.partition("{")
        if name not in _WANTED_METRICS:
            continue
        label_value = labels.rstrip("}").split("=", 1)[1].strip('"') if labels else ""
        try:
            parsed.setdefault(name, {})[label_value] = float(value)
        except ValueError:
            continue
    return parsed
