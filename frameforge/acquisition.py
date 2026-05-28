"""Per-camera grab worker.

- Discover the camera, apply ``.pfs`` (or YAML defaults), apply GigE tuning,
  enter the grab loop.
- On disconnect: produce synthetic black frames at fps cadence so the encoder
  sees a continuous stream, while periodically retrying ``open()``. The no-
  drop guarantee requires this in-worker fill rather than a supervisor
  respawn.
- The supervisor only respawns this worker on a true crash.
"""

import logging
import os
import queue
import time

import numpy as np
from pypylon import genicam, pylon

from .config import CameraCfg
from .context import Context
from .shm_ring import FrameRing


_METRIC_INCOMPLETE = "acq.%s.incomplete"
_METRIC_OVERRUN_DROPS = "acq.%s.overrun_drops"
_METRIC_SYNTHETIC = "acq.%s.synthetic"
_METRIC_LOOP_MS_LAST = "acq.%s.loop_ms_last"
_METRIC_LOOP_MS_MAX = "acq.%s.loop_ms_max"
_METRIC_QUEUE_DEPTH = "acq.%s.queue_depth"
_METRIC_RING_FREE = "acq.%s.ring_free"

_SLOT_ACQUIRE_TIMEOUT_S = 1.0
_RECONNECT_INTERVAL_S = 2.0
_INCOMPLETE_LOG_INTERVAL_S = 30.0
_SAMPLE_EVERY_N_FRAMES = 50


def _host_monotonic_ns():
    # time.monotonic_ns() is 3.7+; this manual conversion works on the Jetson's 3.6 too.
    return int(time.monotonic() * 1_000_000_000)


class CameraDisconnect(Exception):
    pass


class Acquisition:
    def __init__(self, context: Context, camera_config: CameraCfg,
                 frame_ring: FrameRing, data_queue) -> None:
        self.context = context
        self.camera_config = camera_config
        self.frame_ring = frame_ring
        self.data_queue = data_queue
        self.logger = logging.getLogger("frameforge.acquisition")

        self._camera = None
        self._frame_period_seconds = 1.0 / context.config.encode.fps
        self._black_frame = np.zeros(frame_ring.shape, dtype=np.uint8)
        self._retrieve_timeout_ms = 3000

    def run(self) -> None:
        camera_id = self.camera_config.id
        self.logger.info("acquisition %s starting", camera_id)
        try:
            while not self.context.drain.is_set():
                try:
                    self._open_and_configure()
                except Exception as error:
                    self.logger.warning(
                        "open failed cam=%s: %s; entering black-frame",
                        camera_id, error)
                    self._black_frame_until_reconnect()
                    continue

                try:
                    self._grab_loop()
                except CameraDisconnect as disconnect_reason:
                    self.logger.warning(
                        "camera %s disconnected: %s", camera_id, disconnect_reason)
                    self._safe_close()
                    self._black_frame_until_reconnect()
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
        incomplete_burst_count = 0
        incomplete_latest_code = 0
        incomplete_latest_msg = ""
        last_incomplete_log_at = time.monotonic()

        camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        self.logger.info("grab loop %s started", camera_id)
        try:
            while not self.context.drain.is_set():
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
                        incomplete_burst_count += 1
                        incomplete_latest_code = result.GetErrorCode()
                        incomplete_latest_msg = result.GetErrorDescription()
                        now_mono = time.monotonic()
                        if now_mono - last_incomplete_log_at >= _INCOMPLETE_LOG_INTERVAL_S:
                            self.logger.warning(
                                "incomplete cam=%s count=%d in last %ds (latest code=%s msg=%s)",
                                camera_id, incomplete_burst_count,
                                int(now_mono - last_incomplete_log_at),
                                incomplete_latest_code, incomplete_latest_msg)
                            incomplete_burst_count = 0
                            last_incomplete_log_at = now_mono
                        continue

                    try:
                        slot_index = frame_ring.get_free(
                            timeout=_SLOT_ACQUIRE_TIMEOUT_S)
                    except queue.Empty:
                        metrics.incr(_METRIC_OVERRUN_DROPS % camera_id)
                        self.logger.warning(
                            "ring full -> dropped cam=%s", camera_id)
                        continue

                    np.copyto(frame_ring.view(slot_index), result.GetArray())
                    data_queue.put((
                        slot_index,
                        int(result.GetTimeStamp()),
                        _host_monotonic_ns(),
                    ))
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

    def _black_frame_until_reconnect(self):
        camera_id = self.camera_config.id
        metrics = self.context.metrics
        frame_ring = self.frame_ring
        data_queue = self.data_queue
        frame_period = self._frame_period_seconds

        self.logger.warning("black-frame fill engaged cam=%s", camera_id)
        next_emit_at = time.monotonic()
        last_reconnect_at = 0.0

        while not self.context.drain.is_set():
            try:
                slot_index = frame_ring.get_free(timeout=0.5)
                np.copyto(frame_ring.view(slot_index), self._black_frame)
                # hw_ts=0 marks a synthetic frame for the encoder.
                data_queue.put((slot_index, 0, _host_monotonic_ns()))
                metrics.incr(_METRIC_SYNTHETIC % camera_id)
            except queue.Empty:
                metrics.incr(_METRIC_OVERRUN_DROPS % camera_id)
            next_emit_at += frame_period

            now = time.monotonic()
            if now - last_reconnect_at >= _RECONNECT_INTERVAL_S:
                last_reconnect_at = now
                try:
                    self._open_and_configure()
                    self.logger.info("camera %s reconnected", camera_id)
                    return
                except Exception as reconnect_error:
                    self.logger.debug(
                        "reconnect failed cam=%s: %s",
                        camera_id, reconnect_error)

            sleep_remaining = next_emit_at - time.monotonic()
            if sleep_remaining > 0:
                time.sleep(min(sleep_remaining, 0.25))

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
