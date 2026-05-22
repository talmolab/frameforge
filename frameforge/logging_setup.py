"""Process-wide logging: rotating file + stdout. Call once per process."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s %(levelname)-7s %(processName)s %(name)s: %(message)s"


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:          # already configured (e.g. re-import in a worker)
        return
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(_FMT))
    root.addHandler(stream)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fileh = RotatingFileHandler(
            os.path.join(log_dir, "frameforge.log"),
            maxBytes=20 * 1024 * 1024, backupCount=5,
        )
        fileh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fileh)
    except OSError as e:        # log dir not writable → stdout only
        root.warning("file logging disabled (%s): %s", log_dir, e)
