"""Entrypoint: ``python -m frameforge``."""

import os
import shutil

from .core.paths import PROM_DIR

os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", PROM_DIR)

import logging
import multiprocessing

from .config import load_config
from .core.logging_setup import setup_logging
from .core.paths import SCRATCH_DIR
from .core.supervisor import Supervisor

logger = logging.getLogger("frameforge.bootstrap")

_MIN_VALID_MP4_BYTES = 1 * 1024 * 1024


def _prepare_prom_dir() -> None:
    shutil.rmtree(PROM_DIR, ignore_errors=True)
    os.makedirs(PROM_DIR, exist_ok=True)


def _prune_scratch_orphans() -> None:
    if not os.path.isdir(SCRATCH_DIR):
        return

    parts_pruned = 0
    corrupt_pruned = 0
    sidecars_pruned = 0
    for directory, _, filenames in os.walk(SCRATCH_DIR):
        names = set(filenames)
        for filename in filenames:
            full_path = os.path.join(directory, filename)
            if filename.endswith(".part"):
                try:
                    os.remove(full_path)
                    parts_pruned += 1
                except OSError:
                    pass
            elif filename.endswith(".mp4"):
                try:
                    if os.path.getsize(full_path) < _MIN_VALID_MP4_BYTES:
                        os.remove(full_path)
                        corrupt_pruned += 1
                except OSError:
                    pass
            elif filename.endswith(".h5"):
                mp4_name = filename[:-len(".h5")] + ".mp4"
                if mp4_name not in names:
                    try:
                        os.remove(full_path)
                        sidecars_pruned += 1
                    except OSError:
                        pass

    if parts_pruned or corrupt_pruned or sidecars_pruned:
        logger.warning(
            "pruned %d orphan .part + %d suspect .mp4 (<%d bytes) + %d dangling .h5 from scratch=%s",
            parts_pruned, corrupt_pruned, _MIN_VALID_MP4_BYTES, sidecars_pruned, SCRATCH_DIR)


def main() -> None:
    multiprocessing.set_start_method("fork", force=True)
    config = load_config()
    setup_logging()

    _prepare_prom_dir()
    _prune_scratch_orphans()

    Supervisor(config).run()


if __name__ == "__main__":
    main()
