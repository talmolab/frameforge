"""Worker process wrapper: spawn, pin affinity, liveness check."""

import logging
import multiprocessing
import os

logger = logging.getLogger("frameforge.worker")


class WorkerFailure(RuntimeError):
    def __init__(self, name: str, exitcode: int) -> None:
        super().__init__(f"worker {name!r} exitcode={exitcode}")
        self.worker_name = name
        self.exitcode = exitcode


class Worker:
    def __init__(self, name: str, instance, affinity: set[int] | None = None) -> None:
        self.name = name
        self.instance = instance
        self.affinity = affinity
        self.process = None
        self.restart_count = 0
        self.next_restart_ok_at = 0.0

    def start(self) -> None:
        self.process = multiprocessing.Process(
            target=self.instance.run, name=self.name, daemon=False)
        self.process.start()
        if self.affinity and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(self.process.pid, self.affinity)
            except OSError as error:
                logger.warning(
                    "sched_setaffinity failed worker=%s pid=%d cpus=%s err=%s",
                    self.name, self.process.pid, sorted(self.affinity), error)

    def alive(self) -> bool:
        return self.process is not None and self.process.is_alive()
