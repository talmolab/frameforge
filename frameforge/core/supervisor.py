"""Builds the worker graph, spawns it, restarts crashed workers, drives a
graceful drain on signal.

- In-worker recovery handles *expected* failures (camera disconnect → black-
  frame fill). Lives in acquisition.py because the no-drop guarantee needs the
  worker to keep producing during disconnects.
- This supervisor's watchdog handles *unexpected* failures (worker process
  death). Exponential backoff respawn; the rest keep running.
- Supervisor-level crashes are handled by systemd ``Restart=on-failure``.
"""

import datetime
import logging
import multiprocessing
import signal
import time
from typing import List

from prometheus_client import multiprocess

from .config import Config
from .context import Context
from ..metrics.defs import soft_drain_pending, worker_restarts
from .shm_ring import FrameRing
from ..workers.acquisition import Acquisition
from ..workers.broadcast import Broadcast
from ..workers.encoder import Encoder
from ..workers.host_sampler import HostSampler
from ..metrics.exposition import Metrics
from ..workers.transfer import Transfer

logger = logging.getLogger("frameforge.supervisor")

_BACKOFF_CAP_SECONDS = 30
_DRAIN_JOIN_SECONDS = 3700


class Worker:
    def __init__(self, name: str, instance) -> None:
        self.name = name
        self.instance = instance
        self.process = None
        self.restart_count = 0
        self.next_restart_ok_at = 0.0

    def start(self) -> None:
        self.process = multiprocessing.Process(
            target=self.instance.run, name=self.name, daemon=False)
        self.process.start()

    def alive(self) -> bool:
        return self.process is not None and self.process.is_alive()


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._manager = multiprocessing.Manager()

        session_name = config.session_name or \
            datetime.datetime.now().strftime("%Y-%m-%d") + "-Frameforge"

        self.context = Context(
            config=config,
            drain=multiprocessing.Event(),
            hard_drain=multiprocessing.Event(),
            session_name=session_name,
        )
        self.workers: List[Worker] = []
        self._frame_rings: List[FrameRing] = []
        self._worker_pids = self._manager.dict()

        logger.info("session=%s", session_name)

    def build(self) -> None:
        config = self.config
        broadcast_enabled = config.broadcast.enabled

        for camera in config.cameras:
            frame_ring = FrameRing(
                config.ring_slots, config.height, config.width, config.channels)
            data_queue = multiprocessing.Queue(maxsize=config.queue_depth)
            self._frame_rings.append(frame_ring)

            broadcast_ring = None
            broadcast_queue = None
            if broadcast_enabled:
                broadcast_ring = FrameRing(
                    4, config.height, config.width, config.channels)
                broadcast_queue = multiprocessing.Queue(maxsize=8)
                self._frame_rings.append(broadcast_ring)

            self.workers.append(Worker(
                "acq:%s" % camera.id,
                Acquisition(self.context, camera, frame_ring, data_queue,
                            broadcast_ring, broadcast_queue)))
            self.workers.append(Worker(
                "enc:%s" % camera.id,
                Encoder(self.context, camera, frame_ring, data_queue)))

            if broadcast_enabled:
                self.workers.append(Worker(
                    "bcast:%s" % camera.id,
                    Broadcast(self.context, camera,
                              broadcast_ring, broadcast_queue)))

        self.workers.append(Worker("transfer", Transfer(self.context)))
        self.workers.append(Worker("metrics", Metrics(self.context)))
        self.workers.append(Worker(
            "host_sampler", HostSampler(self.context, self._worker_pids)))

    def run(self) -> None:
        self._install_signals()
        self.build()
        soft_drain_pending.set(0)

        logger.info("starting %d workers (%d camera(s))",
                    len(self.workers), len(self.config.cameras))
        for worker in self.workers:
            worker.start()
            self._worker_pids[worker.name] = worker.process.pid
            logger.info("started %s pid=%s", worker.name, worker.process.pid)

        while not self.context.drain.is_set():
            now_seconds = time.time()
            for worker in self.workers:
                if worker.alive() or self.context.drain.is_set():
                    continue
                if now_seconds < worker.next_restart_ok_at:
                    continue

                dead_pid = worker.process.pid if worker.process else None
                worker.restart_count += 1
                worker_restarts.labels(worker=worker.name).inc()
                backoff_seconds = min(
                    _BACKOFF_CAP_SECONDS, 2 ** min(worker.restart_count, 5))
                worker.next_restart_ok_at = now_seconds + backoff_seconds

                logger.warning("worker %s died (restart #%d); respawning",
                               worker.name, worker.restart_count)
                if dead_pid is not None:
                    try:
                        multiprocess.mark_process_dead(dead_pid)
                    except Exception:
                        pass
                worker.start()
                self._worker_pids[worker.name] = worker.process.pid
                logger.info("respawned %s pid=%s",
                            worker.name, worker.process.pid)
            time.sleep(1.0)

        self._shutdown()

    # Two-signal drain: SIGTERM (`systemctl stop`) lets encoders finish
    # the current chunk first; SIGINT (`systemctl kill -s INT`) bails the
    # write loop immediately and finalizes whatever partial exists.
    def _install_signals(self) -> None:
        def soft_handler(signum, _frame):
            logger.info("signal %s received soft drain wait for chunk boundary",
                        signum)
            soft_drain_pending.set(1)
            self.context.drain.set()

        def hard_handler(signum, _frame):
            logger.info("signal %s received hard drain immediate", signum)
            soft_drain_pending.set(1)
            self.context.drain.set()
            self.context.hard_drain.set()

        signal.signal(signal.SIGTERM, soft_handler)
        signal.signal(signal.SIGINT, hard_handler)

    def _shutdown(self) -> None:
        if self.context.hard_drain.is_set():
            logger.info("hard drain: encoders exit immediately")
        else:
            logger.info("soft drain: encoders exit at next chunk boundary timeout=%ds",
                        _DRAIN_JOIN_SECONDS)
        deadline = time.time() + _DRAIN_JOIN_SECONDS

        encoder_workers = [w for w in self.workers if w.name.startswith("enc:")]
        for worker in encoder_workers:
            if worker.process is not None:
                worker.process.join(timeout=max(1.0, deadline - time.time()))

        for worker in self.workers:
            if worker.alive():
                logger.info("terminating %s", worker.name)
                worker.process.terminate()

        for worker in self.workers:
            if worker.process is not None and worker.alive():
                worker.process.join(timeout=5.0)

        logger.info("supervisor exit")
