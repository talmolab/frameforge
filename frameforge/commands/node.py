"""Node: every operator verb as a ``@verb``-annotated method returning plain data.

The CLI and the management server both drive this class through the verb
registry. It never prints. Control goes through systemd for the frameforge
unit; intent that must survive a restart lives in the desired-state file.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from collections.abc import Iterator

import yaml

from ..core.paths import CAMERAS_FILE, DESIRED_FILE, HOME_DIR, TENANT_FILE
from .system import SystemAdapter
from .types import ActionResult, CameraState, DesiredState, DiscoveredCamera, Status
from .verbs import Arg, verb

UNIT = "frameforge"
UNITS = ("frameforge", "heartbeat.timer", "mediamtx")
_METRICS_URL = "http://127.0.0.1:9100/metrics"
_METRICS_TIMEOUT_S = 1.0
_WANTED_METRICS = ("acq_camera_alive", "frameforge_uptime_seconds")


class Node:
    def __init__(self, system: SystemAdapter | None = None) -> None:
        self.system = system or SystemAdapter()

    @verb("units, cameras, desired state", mutates=False)
    def status(self) -> Status:
        metrics = _scrape_metrics()
        alive = metrics.get("acq_camera_alive", {})
        cameras = [
            CameraState(id=cam["id"], serial=str(cam.get("serial", "")),
                        alive=None if not metrics else alive.get(cam["id"], 0.0) >= 1)
            for cam in _read_cameras()
        ]
        return Status(
            hostname=socket.gethostname(),
            units={unit: self.system.is_active(unit) for unit in UNITS},
            desired=self.desired(),
            config_hash=_config_hash(),
            uptime_s=metrics.get("frameforge_uptime_seconds", {}).get(""),
            cameras=cameras,
        )

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
        self._set_desired(paused=True)
        return _result("pause", self.system.stop(UNIT))

    @verb("clear pause and start")
    def resume(self) -> ActionResult:
        self._set_desired(paused=False)
        return _result("resume", self.system.start(UNIT))

    @verb("journal for the frameforge unit",
          Arg("n", "lines of history", int, 50), Arg("f", "follow", flag=True),
          mutates=False, streams=True)
    def logs(self, n: int = 50, f: bool = False) -> Iterator[str]:
        return self.system.journal(UNIT, n, f)

    @verb("cameras on the switch vs cameras.yaml", mutates=False)
    def cams(self) -> list[DiscoveredCamera]:
        from pypylon import pylon

        slots = {str(cam.get("serial", "")): cam["id"] for cam in _read_cameras()}
        found = []
        for device in pylon.TlFactory.GetInstance().EnumerateDevices():
            serial = device.GetSerialNumber()
            found.append(DiscoveredCamera(
                serial=serial, model=device.GetModelName(),
                ip=device.GetIpAddress() if device.IsIpAddressAvailable() else "",
                slot=slots.get(serial)))
        return sorted(found, key=lambda cam: (cam.slot or "~", cam.serial))

    @verb("pull and reinstall; restarts if running", Arg("ref", "git ref", default="main"))
    def update(self, ref: str = "main") -> ActionResult:
        installer = os.path.join(HOME_DIR, "deploy", "scripts", "install-frameforge.sh")
        if not os.path.isfile(installer):
            return ActionResult("update", False, f"installer not found at {installer}")
        if os.geteuid() != 0:
            return ActionResult("update", False, "update must run as root")

        # Run a copy: the installer hard-resets the checkout it lives in.
        with open(installer) as src, tempfile.NamedTemporaryFile(
                "w", suffix=".sh", delete=False) as copy:
            shutil.copyfileobj(src, copy)
            copy_path = copy.name
        try:
            result = subprocess.run(
                ["bash", copy_path],
                env={**os.environ, "GIT_REF": ref, "FF_HOME": HOME_DIR},
                capture_output=True, text=True)
        finally:
            os.remove(copy_path)
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
        return ActionResult("update", result.returncode == 0, tail)

    def desired(self) -> DesiredState:
        try:
            with open(DESIRED_FILE) as raw:
                return DesiredState(**json.load(raw))
        except (OSError, ValueError, TypeError):
            return DesiredState()

    def _set_desired(self, **changes) -> None:
        desired = dataclasses.replace(self.desired(), **changes)
        os.makedirs(os.path.dirname(DESIRED_FILE), exist_ok=True)
        tmp_path = DESIRED_FILE + ".tmp"
        with open(tmp_path, "w") as raw:
            json.dump(dataclasses.asdict(desired), raw, indent=1)
            raw.write("\n")
        os.replace(tmp_path, DESIRED_FILE)


def _result(name: str, outcome: tuple[int, str]) -> ActionResult:
    returncode, output = outcome
    return ActionResult(name, returncode == 0, output)


def _read_cameras() -> list[dict]:
    try:
        with open(CAMERAS_FILE) as raw:
            return (yaml.safe_load(raw) or {}).get("cameras", [])
    except OSError:
        return []


def _config_hash() -> str:
    digest = hashlib.sha256()
    for path in (TENANT_FILE, CAMERAS_FILE):
        try:
            with open(path, "rb") as raw:
                digest.update(raw.read())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:12]


def _scrape_metrics() -> dict[str, dict[str, float]]:
    """{metric: {label_value: value}}; label value is "" for unlabelled gauges."""
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
