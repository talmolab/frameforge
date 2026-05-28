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

from .acquisition import Acquisition
from .config import Config
from .context import Context, MetricsRegistry
from .encoder import Encoder
from .eventbus import EventBus
from .host_sampler import HostSampler
from .metrics import Metrics
from .shm_ring import FrameRing
from .transfer import Transfer

logger = logging.getLogger("frameforge.supervisor")

_BACKOFF_CAP_SECONDS = 30
_DRAIN_JOIN_SECONDS = 60

_METRIC_WORKER_RESTARTS = "worker_restarts.%s"


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

        now = datetime.datetime.now()
        recording_start = now.replace(minute=0, second=0, microsecond=0)
        recording_start_str = recording_start.strftime("%Y-%m-%d-%H-%M-%S")
        session_name = config.recording.session_name or \
            now.strftime("%Y-%m-%d") + "-Frameforge"

        self.context = Context(
            config=config,
            drain=multiprocessing.Event(),
            metrics=MetricsRegistry(
                self._manager.dict(), self._manager.dict()),
            session_name=session_name,
            recording_start=recording_start,
            recording_start_str=recording_start_str,
        )
        self.workers: List[Worker] = []
        self._frame_rings: List[FrameRing] = []
        self._worker_pids = self._manager.dict()

        logger.info("session=%s recording_start=%s",
                    session_name, recording_start_str)

    def build(self) -> None:
        config = self.config

        for camera in config.cameras:
            frame_ring = FrameRing(
                config.ring_slots, config.height, config.width, config.channels)
            data_queue = multiprocessing.Queue(maxsize=config.queue_depth)
            self._frame_rings.append(frame_ring)

            self.workers.append(Worker(
                "acq:%s" % camera.id,
                Acquisition(self.context, camera, frame_ring, data_queue)))
            self.workers.append(Worker(
                "enc:%s" % camera.id,
                Encoder(self.context, camera, frame_ring, data_queue)))

        self.workers.append(Worker("transfer", Transfer(self.context)))
        self.workers.append(Worker("metrics", Metrics(self.context)))
        self.workers.append(Worker("eventbus", EventBus(self.context)))
        self.workers.append(Worker(
            "host_sampler", HostSampler(self.context, self._worker_pids)))

    def run(self) -> None:
        self._install_signals()
        self.build()

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

                worker.restart_count += 1
                self.context.metrics.gauge(
                    _METRIC_WORKER_RESTARTS % worker.name, worker.restart_count)
                backoff_seconds = min(
                    _BACKOFF_CAP_SECONDS, 2 ** min(worker.restart_count, 5))
                worker.next_restart_ok_at = now_seconds + backoff_seconds

                logger.warning("worker %s died (restart #%d); respawning",
                               worker.name, worker.restart_count)
                worker.start()
                self._worker_pids[worker.name] = worker.process.pid
                logger.info("respawned %s pid=%s",
                            worker.name, worker.process.pid)
            time.sleep(1.0)

        self._shutdown()

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            logger.info("signal %s received -> draining", signum)
            self.context.drain.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _shutdown(self) -> None:
        logger.info("draining: waiting up to %ds for workers to finalize",
                    _DRAIN_JOIN_SECONDS)
        deadline = time.time() + _DRAIN_JOIN_SECONDS

        for worker in self.workers:
            if worker.process is not None:
                worker.process.join(timeout=max(1.0, deadline - time.time()))

        for worker in self.workers:
            if worker.alive():
                logger.warning("force-terminating %s", worker.name)
                worker.process.terminate()

        logger.info("supervisor exit")
