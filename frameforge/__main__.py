"""Entrypoint: ``python -m frameforge``."""

import os
import shutil

# Must be set BEFORE prom_client is imported (transitively via supervisor →
# workers → metrics_defs). systemd unit sets this explicitly; the setdefault
# covers ad-hoc CLI runs.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/frameforge-prom")

import logging
import multiprocessing

from .core.config import load_config
from .core.logging_setup import setup_logging
from .core.supervisor import Supervisor

logger = logging.getLogger("frameforge.bootstrap")

_MIN_VALID_MP4_BYTES = 1 * 1024 * 1024


def _prepare_prom_dir() -> None:
    prom_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not prom_dir:
        return
    shutil.rmtree(prom_dir, ignore_errors=True)
    os.makedirs(prom_dir, exist_ok=True)


def _prune_scratch_orphans(scratch_dir: str) -> None:
    if not os.path.isdir(scratch_dir):
        return

    parts_pruned = 0
    corrupt_pruned = 0
    for directory, _subdirs, filenames in os.walk(scratch_dir):
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

    if parts_pruned or corrupt_pruned:
        logger.warning(
            "pruned %d orphan .part + %d suspect .mp4 (<%d bytes) from scratch=%s",
            parts_pruned, corrupt_pruned, _MIN_VALID_MP4_BYTES, scratch_dir)


def main() -> None:
    multiprocessing.set_start_method("fork", force=True)
    config = load_config()
    setup_logging(config.log_dir)

    _prepare_prom_dir()
    _prune_scratch_orphans(config.scratch_dir)

    Supervisor(config).run()


if __name__ == "__main__":
    main()
