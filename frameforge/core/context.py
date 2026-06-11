"""Shared worker dependencies, built once by the supervisor and passed to every
worker. Holds the live config, two drain Events, and the session_name
(snapshotted at boot; per-day recording_start_str is derived later by the
encoder per chunk open).

Drain events follow the two-signal model from docs/deployment.md:
- ``drain`` is set on SIGTERM. Encoder finishes the current chunk and then
  exits. Acquisition keeps producing so the encoder has frames.
- ``hard_drain`` is set on SIGINT (also sets ``drain``). Every worker exits
  ASAP; encoder finalizes the partial .mp4 and stops.

Metrics live in ``metrics_defs`` and are written directly via prom_client
multi-process mode — no registry passed through Context.
"""

from dataclasses import dataclass
from typing import Any

from .config import Config


@dataclass
class Context:
    config: Config
    drain: Any
    hard_drain: Any
    session_name: str
