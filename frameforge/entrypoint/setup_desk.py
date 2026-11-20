"""Desk mode: a fresh box, no cameras yet. Collect the rig's identity and storage,
prove the storage connection, write the three config files, run the box scripts.
"""

import os
import secrets
import socket
import subprocess
import uuid
import zoneinfo
from dataclasses import dataclass

import yaml

from ..core.paths import CAMERAS_FILE, DESIRED_FILE, TENANT_FILE
from .flows import Prompter, Question, collect
from .handlers import write_desired
from .setup import EXAMPLE_TENANT, SECRETS_FILE, SERVICE_USER, run_box_scripts, write_text, write_yaml
from .types import ActionResult

_SMB_ATTEMPTS = 3
_SMB_PORT = 445


def questions() -> list[Question]:
    nics = _list_nics()
    return [
        Question("hostname", "Rig hostname", "shows in the hub and in heartbeat files",
                 default=socket.gethostname()),
        Question("timezone", "Timezone", "chunk folders follow this clock",
                 default=_current_timezone(), validate=_check_timezone),
        Question("camera_nic", "Camera NIC", "the port the camera switch will plug into",
                 default=_camera_nic_default(nics), choices=tuple(nic.name for nic in nics),
                 describe={nic.name: nic.description for nic in nics}),
        Question("password", f"Password for {SERVICE_USER}", "ssh and sudo on this box",
                 secret=True, repeat=True),
        Question("smb_server", "Storage server", "e.g. pool1.vast.salk.edu",
                 default="pool1.vast.salk.edu"),
        Question("smb_share", "Share", "e.g. talmo", default="talmo"),
        Question("smb_root", "Root folder on the share", "e.g. cdracos/frameforge",
                 default="cdracos/frameforge"),
    ]


def storage_questions() -> list[Question]:
    return [
        Question("smb_user", "Storage username"),
        Question("smb_pass", "Storage password", secret=True),
    ]


def run(node, prompter: Prompter) -> ActionResult:
    answers = collect(questions(), prompter)
    credentials = _connect_storage(answers, prompter)
    if credentials is None:
        return ActionResult("setup", False, "storage connection failed; nothing written")
    answers.update(credentials)

    device_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    _write_secrets(answers, device_id, token)
    _write_tenant(answers)
    if not os.path.exists(CAMERAS_FILE):
        write_yaml(CAMERAS_FILE, {"cameras": []})
    if not os.path.exists(DESIRED_FILE):
        write_desired()

    from ..config import load_tenant_config
    try:
        load_tenant_config()
    except ValueError as error:
        return ActionResult("setup", False, f"tenant.yaml invalid: {error}")

    scripts = run_box_scripts(
        answers["hostname"], answers["camera_nic"], answers["timezone"], answers["password"])
    return ActionResult("setup", True, "\n".join([
        f"wrote {TENANT_FILE}, {CAMERAS_FILE}, {SECRETS_FILE}",
        f"device_id={device_id}",
        f"token={token}   (register with the hub; not shown again)",
        scripts,
        "next: cable the cameras, then `sudo ff setup --mode rack`",
    ]))


def _connect_storage(answers: dict, prompter: Prompter) -> dict | None:
    for attempt in range(_SMB_ATTEMPTS):
        credentials = collect(storage_questions(), prompter)
        problem = _smb_probe(answers["smb_server"], answers["smb_share"], credentials)
        if problem is None:
            prompter.say("storage: connected")
            return credentials
        prompter.say(f"storage: {problem}")
        if attempt + 1 == _SMB_ATTEMPTS or not prompter.confirm("Retry credentials?"):
            return None
    return None


def _smb_probe(server: str, share: str, credentials: dict) -> str | None:
    """None when the share lists with these credentials; otherwise a one-line reason."""
    import smbclient

    try:
        smbclient.register_session(
            server, username=credentials["smb_user"], password=credentials["smb_pass"],
            port=_SMB_PORT)
        smbclient.listdir(f"\\\\{server}\\{share}")
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    finally:
        try:
            smbclient.delete_session(server)
        except Exception:
            pass
    return None


def _write_secrets(answers: dict, device_id: str, token: str) -> None:
    write_text(SECRETS_FILE, "".join(f"{key}={value}\n" for key, value in (
        ("SMB_USER", answers["smb_user"]),
        ("SMB_PASS", answers["smb_pass"]),
        ("FF_DEVICE_ID", device_id),
        ("FF_TOKEN", token),
    )), mode=0o600)


def _write_tenant(answers: dict) -> None:
    with open(EXAMPLE_TENANT) as raw:
        tenant = yaml.safe_load(raw)
    tenant.setdefault("transfer", {})["storage"] = {
        "kind": "smb", "server": answers["smb_server"],
        "share": answers["smb_share"], "root": answers["smb_root"]}
    tenant.setdefault("encode", {})["timezone"] = answers["timezone"]
    write_yaml(TENANT_FILE, tenant)


@dataclass(frozen=True)
class Nic:
    name: str
    speed_mbps: int | None
    cable: bool
    address: str

    @property
    def description(self) -> str:
        speed = f"{self.speed_mbps / 1000:g} Gb/s" if self.speed_mbps else "speed unknown"
        state = f"cable in, address {self.address}" if self.address else \
            "cable in, no address" if self.cable else "no cable"
        role = "uplink" if self.address else "SFP+" if (self.speed_mbps or 0) >= 10000 else ""
        return f"{self.name:12} {speed:14} {state:34} {role}".rstrip()


def _list_nics() -> list[Nic]:
    addrs = _run(["ip", "-4", "-o", "addr", "show"])
    ipv4 = {}
    for line in addrs.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            ipv4[fields[1]] = fields[3].split("/")[0]

    nics = []
    for line in _run(["ip", "-o", "link", "show"]).splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if not name.startswith(("en", "eth")):
            continue
        nics.append(Nic(name, _sysfs_int(name, "speed"), _sysfs_int(name, "carrier") == 1,
                        ipv4.get(name, "")))
    return nics


def _sysfs_int(nic: str, attribute: str) -> int | None:
    try:
        with open(f"/sys/class/net/{nic}/{attribute}") as raw:
            value = int(raw.read().strip())
    except (OSError, ValueError):
        return None
    return value if value >= 0 else None


def _camera_nic_default(nics: list[Nic]) -> str:
    """Fastest port without a cable: the uplink is cabled, the camera switch is not yet."""
    candidates = [nic for nic in nics if not nic.cable and not nic.address] or nics
    if not candidates:
        return ""
    return max(candidates, key=lambda nic: nic.speed_mbps or 0).name


def _current_timezone() -> str:
    return _run(["timedatectl", "show", "-p", "Timezone", "--value"]).strip() or "UTC"


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True).stdout
    except OSError:
        return ""


def _check_timezone(value: str) -> str | None:
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return f"unknown timezone {value!r}"
    return None
