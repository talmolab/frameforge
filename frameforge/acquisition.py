"""Per-camera grab worker.

- Discover the camera, apply ``.pfs`` (or YAML defaults), apply GigE tuning,
  enter the grab loop.
- On disconnect: stop producing frames and retry ``open()`` periodically. The
  encoder sees ``queue.Empty`` and idles until the camera comes back; the
  finalized chunk is short by the disconnect duration. No synthetic fill.
- Drain semantics (see docs/deployment.md): acq ignores soft drain so the
  encoder can finish its current chunk. Acq honors ``hard_drain`` for
  immediate exit, and exits on supervisor-driven termination after encoders
  have completed during a soft drain.
- The supervisor only respawns this worker on a true crash.
"""

import logging
import os
import queue
import time

import numpy as np
from pypylon import genicam, pylon

from .broadcast import METRIC_DROPPED as _METRIC_BCAST_DROPPED
from .config import CameraCfg
from .context import Context
from .log_utils import BurstAggregator
from .shm_ring import FrameRing


_METRIC_INCOMPLETE = "acq.%s.incomplete"
_METRIC_OVERRUN_DROPS = "acq.%s.overrun_drops"
_METRIC_LOOP_MS_LAST = "acq.%s.loop_ms_last"
_METRIC_LOOP_MS_MAX = "acq.%s.loop_ms_max"
_METRIC_QUEUE_DEPTH = "acq.%s.queue_depth"
_METRIC_RING_FREE = "acq.%s.ring_free"

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

        self._camera = None
        self._retrieve_timeout_ms = 3000

    def run(self) -> None:
        camera_id = self.camera_config.id
        self.logger.info("acquisition %s starting", camera_id)
        try:
            while not self.context.hard_drain.is_set():
                try:
                    self._open_and_configure()
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
                    self._safe_close()
                    if self.context.hard_drain.wait(_RECONNECT_INTERVAL_S):
                        break
        finally:
            self._safe_close()
            self.logger.info("acquisition %s stopped", camera_id)

    def _open_and_configure(self):
        camera = self._discover()
        camera.Open()
        self._camera = camera

        if self.camera_config.pfs and os.path.isfile(self.camera_config.pfs):
            pylon.FeaturePersistence.Load(
                self.camera_config.pfs, camera.GetNodeMap(), True)
            self.logger.info("cam=%s applied .pfs %s",
                             self.camera_config.id, self.camera_config.pfs)
        else:
            self._apply_yaml_defaults(camera)

        # GigE tuning set after .pfs/defaults so YAML wins.
        acq_config = self.context.config.acq
        _try_set(camera, "GevSCPSPacketSize", acq_config.packet_size)
        _try_set(camera, "GevSCPD",           acq_config.inter_packet_delay_ns)
        try:
            camera.MaxNumBuffer.SetValue(acq_config.max_num_buffer)
        except Exception:
            pass

        self._retrieve_timeout_ms = (
            acq_config.retrieve_timeout_ms
            or _heartbeat_ms(camera)
            or 3000
        )
        self.logger.info(
            "cam=%s open serial=%s retrieve_ms=%d",
            self.camera_config.id,
            camera.GetDeviceInfo().GetSerialNumber(),
            self._retrieve_timeout_ms,
        )

    def _discover(self):
        transport_layer_factory = pylon.TlFactory.GetInstance()
        if self.camera_config.serial:
            device_info = pylon.DeviceInfo()
            device_info.SetSerialNumber(self.camera_config.serial)
            return pylon.InstantCamera(
                transport_layer_factory.CreateDevice(device_info))
        return pylon.InstantCamera(
            transport_layer_factory.CreateFirstDevice())

    def _apply_yaml_defaults(self, camera):
        config = self.context.config
        _try_set(camera, "PixelFormat",
                 "Mono8" if config.channels == 1 else "RGB8")
        _try_set(camera, "Width",  config.width)
        _try_set(camera, "Height", config.height)
        try:
            camera.AcquisitionFrameRateEnable.SetValue(True)
        except Exception:
            pass
        if not _try_set(camera, "AcquisitionFrameRateAbs", config.encode.fps):
            _try_set(camera, "AcquisitionFrameRate", config.encode.fps)

    def _grab_loop(self):
        camera = self._camera
        camera_id = self.camera_config.id
        metrics = self.context.metrics
        frame_ring = self.frame_ring
        data_queue = self.data_queue
        retrieve_timeout_ms = self._retrieve_timeout_ms

        iteration_count = 0
        loop_ms_window_max = 0.0
        incomplete_agg = BurstAggregator()
        ring_full_agg = BurstAggregator()

        camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        try:
            while not self.context.hard_drain.is_set():
                loop_start_mono = time.monotonic()
                try:
                    result = camera.RetrieveResult(
                        retrieve_timeout_ms,
                        pylon.TimeoutHandling_ThrowException)
                except genicam.GenericException as pylon_error:
                    raise CameraDisconnect(str(pylon_error))

                try:
                    if not result.GrabSucceeded():
                        metrics.incr(_METRIC_INCOMPLETE % camera_id)
                        snapshot = incomplete_agg.event(
                            latest=(result.GetErrorCode(),
                                    result.GetErrorDescription()))
                        if snapshot is not None:
                            burst_count, elapsed_s, (code, msg) = snapshot
                            self.logger.warning(
                                "incomplete frames cam=%s count=%d in_last=%ds code=%s msg=%s",
                                camera_id, burst_count, elapsed_s, code, msg)
                        continue

                    try:
                        slot_index = frame_ring.get_free(
                            timeout=_SLOT_ACQUIRE_TIMEOUT_S)
                    except queue.Empty:
                        metrics.incr(_METRIC_OVERRUN_DROPS % camera_id)
                        snapshot = ring_full_agg.event()
                        if snapshot is not None:
                            burst_count, elapsed_s, _ = snapshot
                            self.logger.warning(
                                "ring full, dropping frames cam=%s count=%d in_last=%ds",
                                camera_id, burst_count, elapsed_s)
                        continue

                    frame_array = result.GetArray()
                    np.copyto(frame_ring.view(slot_index), frame_array)
                    data_queue.put(slot_index)

                    if (self.broadcast_ring is not None
                            and iteration_count % _BROADCAST_SUBSAMPLE_EVERY == 0):
                        self._tee_broadcast(frame_array, camera_id, metrics)
                finally:
                    result.Release()

                loop_ms = (time.monotonic() - loop_start_mono) * 1000.0
                if loop_ms > loop_ms_window_max:
                    loop_ms_window_max = loop_ms
                iteration_count += 1
                if iteration_count % _SAMPLE_EVERY_N_FRAMES == 0:
                    metrics.gauge(_METRIC_LOOP_MS_LAST % camera_id, loop_ms)
                    metrics.gauge(_METRIC_LOOP_MS_MAX % camera_id, loop_ms_window_max)
                    metrics.gauge(_METRIC_QUEUE_DEPTH % camera_id, _safe_qsize(data_queue))
                    metrics.gauge(_METRIC_RING_FREE % camera_id, frame_ring.free_count())
                    loop_ms_window_max = 0.0
        finally:
            try:
                camera.StopGrabbing()
            except Exception:
                pass

    def _tee_broadcast(self, frame_array, camera_id, metrics):
        try:
            bcast_slot = self.broadcast_ring.get_free(timeout=0)
        except queue.Empty:
            metrics.incr(_METRIC_BCAST_DROPPED % camera_id)
            return
        try:
            np.copyto(self.broadcast_ring.view(bcast_slot), frame_array)
            self.broadcast_queue.put_nowait(bcast_slot)
        except queue.Full:
            self.broadcast_ring.release(bcast_slot)
            metrics.incr(_METRIC_BCAST_DROPPED % camera_id)

    def _safe_close(self):
        if self._camera is None:
            return
        try:
            self._camera.Close()
        except Exception:
            pass
        self._camera = None


def _try_set(camera, node_name, value) -> bool:
    try:
        camera.GetNodeMap().GetNode(node_name).SetValue(value)
        return True
    except Exception:
        return False


def _safe_qsize(q) -> int:
    try:
        return q.qsize()
    except NotImplementedError:
        return -1


def _heartbeat_ms(camera) -> int:
    try:
        return int(camera.GevHeartbeatTimeout.GetValue())
    except Exception:
        return 0
