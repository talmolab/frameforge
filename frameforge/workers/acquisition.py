"""Per-camera grab worker: pulls frames from a source into the ring, tees to broadcast."""

import logging
import queue
import time

import numpy as np

from ..config import CameraCfg
from ..context import Context
from ..core.logging_setup import DEDUP_KEY
from ..core.shm_ring import FrameRing
from ..metrics.defs import (
    acq_camera_alive,
    acq_enc_queue_depth,
    acq_enc_ring_free,
    acq_incomplete,
    acq_loop_duration_seconds,
    acq_missed_frames,
    acq_overrun_drops,
)
from ..sources import make_source
from ..sources.base import SourceDisconnect

_SLOT_ACQUIRE_TIMEOUT_S = 1.0
_RECONNECT_INTERVAL_S = 1.0
_SAMPLE_EVERY_N_FRAMES = 50
_BROADCAST_SUBSAMPLE_EVERY = 5


class Acquisition:
    def __init__(self, context: Context, camera_config: CameraCfg,
                 frame_ring: FrameRing, data_queue,
                 broadcast_ring: FrameRing = None,
                 broadcast_queue=None) -> None:
        self.context = context
        self.camera_config = camera_config
        self.frame_ring = frame_ring
        self.data_queue = data_queue
        self.broadcast_ring = broadcast_ring
        self.broadcast_queue = broadcast_queue
        self.logger = logging.getLogger("frameforge.acquisition")

        self.source = make_source(camera_config, context.config)

    def run(self) -> None:
        camera_id = self.camera_config.id
        metric_alive = acq_camera_alive.labels(cam=camera_id)
        self.logger.info("acquisition %s starting", camera_id)
        try:
            while not self.context.hard_drain.is_set():
                try:
                    self.source.open()
                except Exception as error:
                    metric_alive.set(0)
                    self.logger.warning(
                        "open failed cam=%s err=%s", camera_id, error)
                    if self.context.hard_drain.wait(_RECONNECT_INTERVAL_S):
                        break
                    continue
                metric_alive.set(1)

                try:
                    self._grab_loop()
                except SourceDisconnect as disconnect_reason:
                    self.logger.warning(
                        "camera disconnected cam=%s reason=%s",
                        camera_id, disconnect_reason)
                    self.source.close()
                    metric_alive.set(0)
                    if self.context.hard_drain.wait(_RECONNECT_INTERVAL_S):
                        break
        finally:
            self.source.close()
            metric_alive.set(0)
            self.logger.info("acquisition %s stopped", camera_id)

    def _grab_loop(self):
        camera_id = self.camera_config.id
        source = self.source
        frame_ring = self.frame_ring
        data_queue = self.data_queue

        metric_queue_depth = acq_enc_queue_depth.labels(cam=camera_id)
        metric_ring_free = acq_enc_ring_free.labels(cam=camera_id)
        metric_loop_hist = acq_loop_duration_seconds.labels(cam=camera_id)

        iteration_count = 0
        last_seq = None

        while not self.context.hard_drain.is_set():
            loop_start_ns = time.monotonic_ns()

            frame = source.grab()
            if frame is None:
                acq_incomplete.labels(cam=camera_id).inc()
                continue

            if frame.seq is not None and last_seq is not None:
                gap = frame.seq - last_seq - 1
                if gap > 0:
                    acq_missed_frames.labels(cam=camera_id).inc(gap)
                    self.logger.warning(
                        "missed frames cam=%s gap=%d", camera_id, gap,
                        extra={DEDUP_KEY: ("acq_missed_frames", camera_id)})
            last_seq = frame.seq

            try:
                slot_index = frame_ring.get_free(timeout=_SLOT_ACQUIRE_TIMEOUT_S)
            except queue.Empty:
                acq_overrun_drops.labels(cam=camera_id).inc()
                self.logger.warning(
                    "ring full, dropping frames cam=%s", camera_id,
                    extra={DEDUP_KEY: ("acq_overrun_drops", camera_id)})
                continue

            np.copyto(frame_ring.view(slot_index), frame.array)
            data_queue.put((slot_index, frame.ts_ns))

            if (self.broadcast_ring is not None
                    and iteration_count % _BROADCAST_SUBSAMPLE_EVERY == 0):
                self._tee_broadcast(frame.array)

            loop_ns = time.monotonic_ns() - loop_start_ns
            metric_loop_hist.observe(loop_ns / 1_000_000_000.0)

            iteration_count += 1
            if iteration_count % _SAMPLE_EVERY_N_FRAMES == 0:
                metric_queue_depth.set(_safe_qsize(data_queue))
                metric_ring_free.set(frame_ring.free_count())

    def _tee_broadcast(self, frame_array):
        try:
            bcast_slot = self.broadcast_ring.get_free(timeout=0)
        except queue.Empty:
            return
        try:
            np.copyto(self.broadcast_ring.view(bcast_slot), frame_array)
            self.broadcast_queue.put_nowait(bcast_slot)
        except queue.Full:
            self.broadcast_ring.release(bcast_slot)


# macOS multiprocessing.Queue.qsize() raises NotImplementedError; -1 is
# our "unknown" sentinel for the metric.
def _safe_qsize(q) -> int:
    try:
        return q.qsize()
    except NotImplementedError:
        return -1
