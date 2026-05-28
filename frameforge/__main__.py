"""Entrypoint: ``python -m frameforge``."""

import multiprocessing

from .config import load_config
from .logging_setup import setup_logging
from .supervisor import Supervisor


def main() -> None:
    multiprocessing.set_start_method("fork", force=True)
    config = load_config()
    setup_logging(config.log_dir)
    Supervisor(config).run()


if __name__ == "__main__":
    main()
