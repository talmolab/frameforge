"""Process-wide logging: rotating file + stdout. Call once per process.

Canonical line format: ``YYYY-MM-DD HH:MM:SS LEVEL [worker] message``.
The ``[worker]`` tag comes from ``multiprocessing.Process.name`` (set by the
supervisor when spawning each worker) via ``%(processName)s`` — no
LoggerAdapter wiring required.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)-5s [%(processName)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    root_logger.setLevel(level)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    for noisy in ("smbprotocol", "smbclient", "spnego"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "frameforge.log"),
            maxBytes=20 * 1024 * 1024, backupCount=5,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as error:
        root_logger.warning("file logging disabled (%s): %s", log_dir, error)
