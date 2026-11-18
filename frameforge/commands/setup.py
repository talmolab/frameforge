"""Shared pieces of ``ff setup``: paths, config file writers, the box scripts.

Desk and rack flows live in setup_desk.py and setup_rack.py; Node.setup dispatches.
"""

import os
import shutil
import subprocess
import tempfile

import yaml

from ..core.paths import CAMERAS_FILE, CONFIG_DIR

REPO_DIR = os.environ.get(
    "FF_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
EXAMPLE_TENANT = os.path.join(REPO_DIR, "config", "tenants", "example.yaml")
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.env")
SERVICE_USER = os.environ.get("FF_USER", "talmolab")

_BOOTSTRAP = os.path.join(REPO_DIR, "deploy", "scripts", "bootstrap-box.sh")
_INSTALL = os.path.join(REPO_DIR, "deploy", "scripts", "install-frameforge.sh")

_FIRST_BOOT_FILES = (
    "/etc/sudoers.d/90-frameforge-firstboot",
    "/etc/systemd/system/getty@tty1.service.d/frameforge-firstboot.conf",
    "/etc/profile.d/frameforge-firstboot.sh",
)


def read_cameras() -> list[dict]:
    try:
        with open(CAMERAS_FILE) as raw:
            return (yaml.safe_load(raw) or {}).get("cameras", [])
    except OSError:
        return []


def write_yaml(path: str, data: dict) -> None:
    write_text(path, yaml.safe_dump(data, sort_keys=False, default_flow_style=False))


def write_text(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as raw:
        raw.write(text)
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def run_box_scripts(hostname: str, camera_nic: str, timezone: str, password: str) -> str:
    """bootstrap-box.sh then install-frameforge.sh, unchanged, driven by env.
    The service password is set first so bootstrap's passwd prompt is skipped.
    Scripts run from copies: the installer hard-resets the checkout they live in."""
    if os.geteuid() != 0:
        return "not root: skipped bootstrap + install (run `sudo ff setup` to apply)"
    if subprocess.run(["id", "-u", SERVICE_USER], capture_output=True).returncode != 0:
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", SERVICE_USER], check=True)
    subprocess.run(["chpasswd"], input=f"{SERVICE_USER}:{password}\n", text=True, check=True)
    _end_first_boot()

    env = {**os.environ, "FF_HOSTNAME": hostname, "CAMERA_IFACE": camera_nic,
           "FF_TIMEZONE": timezone, "FF_USER": SERVICE_USER}
    with tempfile.TemporaryDirectory() as scratch:
        for script, args in ((_BOOTSTRAP, ["--with-broadcast"]), (_INSTALL, [])):
            copy = shutil.copy(script, scratch)
            if subprocess.run(["bash", copy, *args], env=env).returncode != 0:
                return f"{os.path.basename(script)} failed; fix and re-run `sudo ff setup`"
    return "bootstrap + install complete"


def _end_first_boot() -> None:
    removed = False
    for path in _FIRST_BOOT_FILES:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    if removed:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
