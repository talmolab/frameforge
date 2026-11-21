"""Rig heartbeat: publish a ``Heartbeat`` record so the hub can find, register and
reach the box. Each rig records under <lab folder>/<hostname>; the heartbeat goes
to <lab folder>/_ff_heartbeat/<hostname>.json, beside the rig folders, so one
folder lists every rig. Run hourly by heartbeat.timer."""

import dataclasses
import datetime
import json
import logging
import os
import posixpath
import socket
import sys
import tempfile
import time

from .config import load_tenant_config
from .core.logging_setup import setup_logging
from .entrypoint import document
from .entrypoint.handlers import git_sha, read_desired, read_session, secret, uplink_ip
from .entrypoint.server import DEFAULT_PORT
from .entrypoint.types import Heartbeat
from .storage import make_storage

_HEARTBEAT_DIR = "_ff_heartbeat"
_DEFAULT_MAX_ATTEMPTS = 20
_DEFAULT_RETRY_SECONDS = 15.0

logger = logging.getLogger("frameforge.heartbeat")


def lab_folder(storage):
    """The rig root's parent: where every rig's folder and _ff_heartbeat live."""
    options = dict(storage.options)
    for key in ("root", "prefix"):
        if key in options:
            options[key] = posixpath.dirname(options[key].rstrip("/"))
    return dataclasses.replace(storage, options=options)


def build(hostname: str, ip: str) -> Heartbeat:
    state = document.state()
    return Heartbeat(
        hostname=hostname,
        ip=ip,
        port=int(os.environ.get("FF_SERVE_PORT", DEFAULT_PORT)),
        device_id=secret("FF_DEVICE_ID"),
        token=secret("FF_TOKEN"),
        timestamp=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        frameforge_sha=git_sha(),
        config_version=state.version,
        config_version_applied=state.version_applied,
        paused=read_desired().paused,
        session=read_session(),
    )


def _write_heartbeat(storage, hostname: str, exclude_prefix: str) -> bool:
    ip = uplink_ip(exclude_prefix)
    if ip is None:
        logger.warning("no routable uplink IP yet")
        return False
    if not storage.ensure_open():
        return False

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(dataclasses.asdict(build(hostname, ip)), tmp, indent=1)
        tmp.write("\n")
        tmp_path = tmp.name

    try:
        storage.put(tmp_path, f"{_HEARTBEAT_DIR}/{hostname}.json")
    except Exception as error:
        logger.warning("heartbeat put failed err=%s", error)
        storage.mark_dead()
        return False
    finally:
        os.remove(tmp_path)

    logger.info("heartbeat written host=%s ip=%s location=%s",
                hostname, ip, storage.location)
    return True


def main() -> int:
    setup_logging()
    max_attempts = int(os.environ.get("HB_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS))
    retry_seconds = float(os.environ.get("HB_RETRY_SECONDS", _DEFAULT_RETRY_SECONDS))

    config = load_tenant_config()
    storage = make_storage(lab_folder(config.transfer.storage))
    hostname = socket.gethostname()

    try:
        for attempt in range(1, max_attempts + 1):
            if _write_heartbeat(storage, hostname, config.acq.gige_subnet):
                return 0
            if attempt < max_attempts:
                logger.warning("heartbeat attempt %d/%d failed; retrying in %.0fs",
                               attempt, max_attempts, retry_seconds)
                time.sleep(retry_seconds)
    finally:
        storage.close()

    logger.error("heartbeat failed after %d attempts", max_attempts)
    return 1


if __name__ == "__main__":
    sys.exit(main())
