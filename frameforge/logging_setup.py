"""Process-wide logging: rotating file + stdout. Call once per process."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)-7s %(processName)s %(name)s: %(message)s"


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    root_logger.setLevel(level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(_FORMAT))
    root_logger.addHandler(stream_handler)

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "frameforge.log"),
            maxBytes=20 * 1024 * 1024, backupCount=5,
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root_logger.addHandler(file_handler)
    except OSError as error:
        root_logger.warning("file logging disabled (%s): %s", log_dir, error)
