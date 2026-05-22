"""Supervisor: build the worker graph, spawn it, restart dead workers, and
drive a graceful drain on signal.

Model: multiprocessing. Per camera → one acquisition + one encoder process
wired by a shared-memory FrameRing + a bounded index queue. Plus shared
transfer, metrics, and event-bus processes. N-camera ready (iterates config).

Restart policy: per-worker. A worker that dies while not draining is respawned
with exponential backoff; the others keep running. Supervisor-level crashes are
covered by systemd ``Restart=on-failure``.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import signal
import time
from typing import Callable, List, Tuple

from . import acquisition, encoder, eventbus, metrics, transfer
from .config import Config
from .shm_ring import FrameRing

log = logging.getLogger("frameforge.supervisor")

_BACKOFF_CAP_S = 30
_DRAIN_JOIN_S = 60          # workers finalize current chunk promptly on drain


class Worker:
    def __init__(self, name: str, target: Callable, args: Tuple):
        self.name = name
        self.target = target
        self.args = args
        self.proc = None            # type: mp.Process
        self.restarts = 0
        self.next_ok = 0.0

    def start(self) -> None:
        self.proc = mp.Process(target=self.target, args=self.args,
                               name=self.name, daemon=False)
        self.proc.start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.drain = mp.Event()
        self._mgr = mp.Manager()
        self.registry = self._mgr.dict()      # shared metrics registry
        self.workers: List[Worker] = []
        self._rings: List[FrameRing] = []      # keep refs alive for the run

    def build(self) -> None:
        cfg = self.cfg
        for cam in cfg.cameras:
            ring = FrameRing(cfg.ring_slots, cfg.height, cfg.width, cfg.channels)
            data_q = mp.Queue(maxsize=cfg.queue_depth)
            self._rings.append(ring)
            self.workers.append(Worker(
                "acq:%s" % cam.id, acquisition.run,
                (cam, cfg, ring, data_q, self.drain, self.registry)))
            self.workers.append(Worker(
                "enc:%s" % cam.id, encoder.run,
                (cam, cfg, ring, data_q, self.drain, self.registry)))
        self.workers.append(Worker(
            "transfer", transfer.run, (cfg, self.drain, self.registry)))
        self.workers.append(Worker(
            "metrics", metrics.run, (cfg, self.registry, self.drain)))
        self.workers.append(Worker(
            "eventbus", eventbus.run, (cfg, self.drain, self.registry)))

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            log.info("signal %s received -> draining", signum)
            self.drain.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self) -> None:
        self._install_signals()
        self.build()
        log.info("starting %d workers (%d camera(s))",
                 len(self.workers), len(self.cfg.cameras))
        for w in self.workers:
            w.start()
            log.info("started %s pid=%s", w.name, w.proc.pid)

        # Watchdog loop.
        while not self.drain.is_set():
            now = time.time()
            for w in self.workers:
                if w.alive() or self.drain.is_set():
                    continue
                if now < w.next_ok:
                    continue
                w.restarts += 1
                self.registry["worker_restarts.%s" % w.name] = w.restarts
                backoff = min(_BACKOFF_CAP_S, 2 ** min(w.restarts, 5))
                w.next_ok = now + backoff
                log.warning("worker %s died (restart #%d); respawning",
                            w.name, w.restarts)
                w.start()
                log.info("respawned %s pid=%s", w.name, w.proc.pid)
            time.sleep(1.0)

        self._shutdown()

    def _shutdown(self) -> None:
        log.info("draining: waiting up to %ds for workers to finalize", _DRAIN_JOIN_S)
        deadline = time.time() + _DRAIN_JOIN_S
        for w in self.workers:
            if w.proc is not None:
                w.proc.join(timeout=max(1.0, deadline - time.time()))
        for w in self.workers:
            if w.alive():
                log.warning("force-terminating %s", w.name)
                w.proc.terminate()
        log.info("supervisor exit")
