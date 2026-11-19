"""Session start time on disk, kept across restarts while the session name is unchanged."""

import json
import os
import time

from .paths import SESSION_FILE


def read() -> dict | None:
    try:
        with open(SESSION_FILE) as raw:
            record = json.load(raw)
        return record if "name" in record and "started" in record else None
    except (OSError, ValueError):
        return None


def begin(name: str) -> float:
    """Record ``name`` as the current session and return its start time (epoch seconds)."""
    current = read()
    if current is not None and current["name"] == name:
        return current["started"]

    started = time.time()
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    tmp_path = SESSION_FILE + ".tmp"
    with open(tmp_path, "w") as raw:
        json.dump({"name": name, "started": started}, raw)
        raw.write("\n")
    os.replace(tmp_path, SESSION_FILE)
    return started
