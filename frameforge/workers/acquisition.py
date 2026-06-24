"""Per-camera grab worker: drives the pylon grab loop and tees to broadcast."""

import logging
import queue
import time

import numpy as np
from pypylon import genicam, pylon

from ..media.camera import Camera
from ..config import CameraCfg
from ..context import Context
from ..core.logging_setup import DEDUP_KEY
from ..core.shm_ring import FrameRing
from ..metrics.defs import (
    acq_enc_queue_depth,
    acq_enc_ring_free,
    acq_incomplete,
    acq_loop_duration_seconds,
    acq_missed_frames,
    acq_overrun_drops,
)

_SLOT_ACQUIRE_TIMEOUT_S = 1.0
_RECONNECT_INTERVAL_S = 1.0
_SAMPLE_EVERY_N_FRAMES = 50
_BROADCAST_SUBSAMPLE_EVERY = 5


class CameraDisconnect(Exception):
    pass


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

        self.camera = Camera(camera_config, context.config)

    def run(self) -> None:
        camera_id = self.camera_config.id
        self.logger.info("acquisition %s starting", camera_id)
        try:
            while not self.context.hard_drain.is_set():
                try:
                    self.camera.open()
                except Exception as error:
                    self.logger.warning(
                        "open failed cam=%s err=%s", camera_id, error)
                    if self.context.hard_drain.wait(_RECONNECT_INTERVAL_S):
                        break
                    continue

                try:
                    self._grab_loop()
                except CameraDisconnect as disconnect_reason:
                    self.logger.warning(
                        "camera disconnected cam=%s reason=%s",
                        camera_id, disconnect_reason)
                    self.camera.close()
                    if self.context.hard_drain.wait(_RECONNECT_INTERVAL_S):
                        break
        finally:
            self.camera.close()
            self.logger.info("acquisition %s stopped", camera_id)

    def _grab_loop(self):
        pylon_camera = self.camera.pylon_camera
        camera_id = self.camera_config.id
        frame_ring = self.frame_ring
        data_queue = self.data_queue
        retrieve_timeout_ms = self.camera.retrieve_timeout_ms

        metric_queue_depth = acq_enc_queue_depth.labels(cam=camera_id)
        metric_ring_free = acq_enc_ring_free.labels(cam=camera_id)
        metric_loop_hist = acq_loop_duration_seconds.labels(cam=camera_id)

        iteration_count = 0
        last_block_id = None

        pylon_camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        try:
            while not self.context.hard_drain.is_set():
                loop_start_ns = time.monotonic_ns()
                try:
                    result = pylon_camera.RetrieveResult(
                        retrieve_timeout_ms,
                        pylon.TimeoutHandling_ThrowException)
                except genicam.GenericException as pylon_error:
                    raise CameraDisconnect(str(pylon_error))

                try:
                    if not result.GrabSucceeded():
                        acq_incomplete.labels(cam=camera_id).inc()
                        self.logger.warning(
                            "incomplete frames cam=%s code=%s msg=%s",
                            camera_id, result.GetErrorCode(),
                            result.GetErrorDescription(),
                            extra={DEDUP_KEY: ("acq_incomplete", camera_id)})
                        continue

                    block_id = result.GetBlockID()
                    if last_block_id is not None:
                        gap = block_id - last_block_id - 1
                        if gap > 0:
                            acq_missed_frames.labels(cam=camera_id).inc(gap)
                            self.logger.warning(
                                "missed frames cam=%s gap=%d",
                                camera_id, gap,
                                extra={DEDUP_KEY: ("acq_missed_frames", camera_id)})
                    last_block_id = block_id

                    try:
                        slot_index = frame_ring.get_free(
                            timeout=_SLOT_ACQUIRE_TIMEOUT_S)
                    except queue.Empty:
                        acq_overrun_drops.labels(cam=camera_id).inc()
                        self.logger.warning(
                            "ring full, dropping frames cam=%s", camera_id,
                            extra={DEDUP_KEY: ("acq_overrun_drops", camera_id)})
                        continue

                    frame_array = result.GetArray()
                    np.copyto(frame_ring.view(slot_index), frame_array)
                    data_queue.put(slot_index)

                    if (self.broadcast_ring is not None
                            and iteration_count % _BROADCAST_SUBSAMPLE_EVERY == 0):
                        self._tee_broadcast(frame_array)
                finally:
                    result.Release()

                loop_ns = time.monotonic_ns() - loop_start_ns
                metric_loop_hist.observe(loop_ns / 1_000_000_000.0)

                iteration_count += 1
                if iteration_count % _SAMPLE_EVERY_N_FRAMES == 0:
                    metric_queue_depth.set(_safe_qsize(data_queue))
                    metric_ring_free.set(frame_ring.free_count())
        finally:
            try:
                pylon_camera.StopGrabbing()
            except Exception:
                pass

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
