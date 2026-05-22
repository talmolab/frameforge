"""Entrypoint: `python -m frameforge`. Loads config, sets up logging, runs."""
from __future__ import annotations

import multiprocessing as mp

from .config import load_config
from .logging_setup import setup_logging
from .supervisor import Supervisor


def main() -> None:
    # fork: children inherit the shared rings/queues/registry (Linux default;
    # forced for consistency and for fork-based smoke tests on macOS).
    mp.set_start_method("fork", force=True)
    cfg = load_config()
    setup_logging(cfg.log_dir)
    Supervisor(cfg).run()


if __name__ == "__main__":
    main()
