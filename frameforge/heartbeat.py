"""Rig heartbeat: publish {hostname, ip, timestamp} under <root>/_ff_heartbeat/
so the fleet console can find the box. Run by heartbeat.timer."""

import datetime
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time

from .config import load_tenant_config
from .core.logging_setup import setup_logging
from .storage import make_storage

_HEARTBEAT_DIR = "_ff_heartbeat"
_DEFAULT_MAX_ATTEMPTS = 20
_DEFAULT_RETRY_SECONDS = 15.0

logger = logging.getLogger("frameforge.heartbeat")


def _uplink_ip(exclude_prefix: str) -> str | None:
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "scope", "global"],
        capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        ip = fields[3].split("/")[0]
        if not ip.startswith(exclude_prefix + "."):
            return ip
    return None


def _write_heartbeat(storage, hostname: str, exclude_prefix: str) -> bool:
    ip = _uplink_ip(exclude_prefix)
    if ip is None:
        logger.warning("no routable uplink IP yet")
        return False
    if not storage.ensure_open():
        return False

    payload = {
        "hostname": hostname,
        "ip": ip,
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(payload, tmp)
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
    storage = make_storage(config.transfer.storage)
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
