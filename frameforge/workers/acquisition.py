"""Per-camera grab worker: drives the pylon grab loop and tees to broadcast."""

import logging
import queue
import time

import numpy as np
from pypylon import genicam, pylon

from ..media.camera import Camera
from ..core.config import CameraCfg
from ..core.context import Context
from ..metrics.helpers import WindowMaxSampler
from ..metrics.defs import (
    acq_incomplete,
    acq_loop_ms_last,
    acq_loop_ms_max,
    acq_missed_frames,
    acq_overrun_drops,
    acq_queue_depth,
    acq_ring_free,
    bcast_dropped,
)
from ..core.shm_ring import FrameRing

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

        metric_queue_depth = acq_queue_depth.labels(cam=camera_id)
        metric_ring_free = acq_ring_free.labels(cam=camera_id)
        metric_bcast_dropped = bcast_dropped.labels(cam=camera_id)
        loop_ms_sampler = WindowMaxSampler(
            every=_SAMPLE_EVERY_N_FRAMES,
            gauge_last=acq_loop_ms_last.labels(cam=camera_id),
            gauge_max=acq_loop_ms_max.labels(cam=camera_id),
        )

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
                        acq_incomplete.inc(
                            latest=(result.GetErrorCode(),
                                    result.GetErrorDescription()),
                            cam=camera_id)
                        continue

                    block_id = result.GetBlockID()
                    if last_block_id is not None:
                        gap = block_id - last_block_id - 1
                        if gap > 0:
                            for _ in range(gap):
                                acq_missed_frames.inc(
                                    latest=(gap,), cam=camera_id)
                    last_block_id = block_id

                    try:
                        slot_index = frame_ring.get_free(
                            timeout=_SLOT_ACQUIRE_TIMEOUT_S)
                    except queue.Empty:
                        acq_overrun_drops.inc(cam=camera_id)
                        continue

                    frame_array = result.GetArray()
                    np.copyto(frame_ring.view(slot_index), frame_array)
                    data_queue.put(slot_index)

                    if (self.broadcast_ring is not None
                            and iteration_count % _BROADCAST_SUBSAMPLE_EVERY == 0):
                        self._tee_broadcast(frame_array, metric_bcast_dropped)
                finally:
                    result.Release()

                loop_ms = (time.monotonic_ns() - loop_start_ns) / 1_000_000.0
                loop_ms_sampler.observe(loop_ms)

                iteration_count += 1
                if iteration_count % _SAMPLE_EVERY_N_FRAMES == 0:
                    metric_queue_depth.set(_safe_qsize(data_queue))
                    metric_ring_free.set(frame_ring.free_count())
        finally:
            try:
                pylon_camera.StopGrabbing()
            except Exception:
                pass

    def _tee_broadcast(self, frame_array, metric_bcast_dropped):
        try:
            bcast_slot = self.broadcast_ring.get_free(timeout=0)
        except queue.Empty:
            metric_bcast_dropped.inc()
            return
        try:
            np.copyto(self.broadcast_ring.view(bcast_slot), frame_array)
            self.broadcast_queue.put_nowait(bcast_slot)
        except queue.Full:
            self.broadcast_ring.release(bcast_slot)
            metric_bcast_dropped.inc()


# macOS multiprocessing.Queue.qsize() raises NotImplementedError; -1 is
# our "unknown" sentinel for the metric.
def _safe_qsize(q) -> int:
    try:
        return q.qsize()
    except NotImplementedError:
        return -1
