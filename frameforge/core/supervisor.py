"""Builds the worker graph, spawns it, restarts crashed workers, drives a
graceful drain on signal.

- Camera disconnect is handled in acquisition.py via close → wait → reopen.
- This supervisor's watchdog handles *unexpected* worker process death:
  exponential backoff respawn; the rest keep running.
- Supervisor-level crashes are handled by systemd (``Restart=no`` today; we
  own the restart policy).
"""

import datetime
import logging
import multiprocessing
import signal
import time

from prometheus_client import multiprocess

from .hardware import get_hardware_spec
from .shm_ring import FrameRing
from ..config import Config
from ..context import Context
from ..metrics.defs import sv_soft_drain_pending, sv_worker_restarts
from ..metrics.exposition import Metrics
from ..metrics.host_sampler import HostSampler
from ..workers.acquisition import Acquisition
from ..workers.broadcast import Broadcast
from ..workers.encoder import Encoder
from ..workers.transfer import Transfer
from ..workers.worker import Worker, WorkerFailure

logger = logging.getLogger("frameforge.supervisor")

_BACKOFF_CAP_SECONDS = 30
_DRAIN_JOIN_SLACK_SECONDS = 100
_BROADCAST_RING_SLOTS = 24


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._manager = multiprocessing.Manager()

        session_name = config.session_name or (
            datetime.datetime.now().strftime("%Y-%m-%d") + config.session_postfix)

        self.context = Context(
            config=config,
            drain=multiprocessing.Event(),
            hard_drain=multiprocessing.Event(),
            session_name=session_name,
        )
        self.workers: list[Worker] = []
        self._frame_rings: list[FrameRing] = []
        self._worker_pids = self._manager.dict()

        logger.info("session=%s", session_name)

    def build(self) -> None:
        config = self.config
        broadcast_enabled = config.broadcast.enabled
        acq = config.acq
        pin_for = get_hardware_spec(config.hardware).pin_function

        for cam_index, camera in enumerate(config.cameras):
            frame_ring = FrameRing(
                acq.ring_slots, acq.height, acq.width, acq.channels)
            data_queue = multiprocessing.Queue(maxsize=acq.ring_slots * 2)
            self._frame_rings.append(frame_ring)

            broadcast_ring = None
            broadcast_queue = None
            if broadcast_enabled:
                broadcast_ring = FrameRing(
                    _BROADCAST_RING_SLOTS, acq.height, acq.width, acq.channels)
                broadcast_queue = multiprocessing.Queue(maxsize=8)
                self._frame_rings.append(broadcast_ring)

            acq_name = "acq:%s" % camera.id
            enc_name = "enc:%s" % camera.id
            self.workers.append(Worker(
                acq_name,
                Acquisition(self.context, camera, frame_ring, data_queue,
                            broadcast_ring, broadcast_queue),
                affinity=pin_for(acq_name, cam_index)))
            self.workers.append(Worker(
                enc_name,
                Encoder(self.context, camera.id, frame_ring, data_queue),
                affinity=pin_for(enc_name, cam_index)))

            if broadcast_enabled:
                bcast_name = "bcast:%s" % camera.id
                self.workers.append(Worker(
                    bcast_name,
                    Broadcast(self.context, camera.id,
                              broadcast_ring, broadcast_queue),
                    affinity=pin_for(bcast_name, cam_index)))

        self.workers.append(Worker(
            "transfer", Transfer(self.context),
            affinity=pin_for("transfer", -1)))
        self.workers.append(Worker(
            "metrics", Metrics(self.context),
            affinity=pin_for("metrics", -1)))
        self.workers.append(Worker(
            "host_sampler", HostSampler(self.context, self._worker_pids),
            affinity=pin_for("host_sampler", -1)))

    def run(self) -> None:
        self._install_signals()
        self.build()
        sv_soft_drain_pending.set(0)

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
                sv_worker_restarts.labels(worker=worker.name).inc()
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
        
    def _install_signals(self) -> None:
        def soft_handler(signum, _frame):
            logger.info("signal %s received soft drain wait for chunk boundary",
                        signum)
            sv_soft_drain_pending.set(1)
            self.context.drain.set()

        def hard_handler(signum, _frame):
            logger.info("signal %s received hard drain immediate", signum)
            sv_soft_drain_pending.set(1)
            self.context.drain.set()
            self.context.hard_drain.set()

        signal.signal(signal.SIGTERM, soft_handler)
        signal.signal(signal.SIGINT, hard_handler)

    def _shutdown(self) -> None:
        drain_join_seconds = (
            self.context.config.encode.chunk_seconds + _DRAIN_JOIN_SLACK_SECONDS)
        if self.context.hard_drain.is_set():
            logger.info("hard drain: encoders exit immediately")
        else:
            logger.info("soft drain: encoders exit at next chunk boundary timeout=%ds",
                        drain_join_seconds)
        deadline = time.time() + drain_join_seconds

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

        failures = [
            WorkerFailure(w.name, w.process.exitcode)
            for w in self.workers
            if w.process is not None and w.process.exitcode not in (None, 0)
        ]

        try:
            self._manager.shutdown()
        except Exception:
            logger.exception("manager.shutdown failed")

        logger.info("supervisor exit")

        if failures:
            raise ExceptionGroup("worker shutdown failures", failures)
