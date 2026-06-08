"""Per-camera live broadcast worker.

- Reads frames from a separate per-camera broadcast ring (written by acq at 10
  fps subsample). Recording ring is untouched; no slot-ownership race.
- Encodes via GStreamer through ``cv2.VideoWriter``. Pipeline pushes to an
  RTSP mount point (``rtsp://<rtsp_host>:<rtsp_port>/<cam_id>``) served by
  MediaMTX or another RTSP server running alongside frameforge.
- Drop-tolerant by design: ``queue leaky=2`` element in the pipeline drops
  oldest buffers if downstream stalls, so the producer (this worker) never
  blocks regardless of viewer state.
- Backend factory mirrors the recording encoder's pattern so x86 backends
  (libx264 CPU on MS-01, hevc_qsv on MS-01 iGPU) drop in cleanly.

Metric naming kept here as public constants because acq imports
``METRIC_DROPPED`` to count broadcast-ring overruns at the producer side.
"""

import abc
import logging
import queue
import time

import cv2

from .config import CameraCfg, Config
from .context import Context
from .shm_ring import FrameRing


METRIC_DROPPED        = "bcast.%s.dropped"
_METRIC_ENCODE_MS_MAX = "bcast.%s.encode_ms_max"
_METRIC_SESSION_ALIVE = "bcast.session_alive"

_BROADCAST_FPS = 10
_DOWNSCALE_WIDTH = 640
_DOWNSCALE_HEIGHT = 512
_SAMPLE_EVERY_N_FRAMES = 50


class BroadcastBackend(abc.ABC):
    @abc.abstractmethod
    def open(self, *, src_width: int, src_height: int, fps: int,
             bitrate_kbps: int, mount_uri: str, cam_label: str) -> None: ...

    @abc.abstractmethod
    def write(self, frame_bgr) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class GStreamerLibx264Backend(BroadcastBackend):
    """CPU H.264 broadcast via cv2.VideoWriter wrapping a GStreamer pipeline.

    Downscales to 640x512, burns in cam id + clock, queues leakily, pushes
    via rtspclientsink. Default backend on Jetson (HW encoder reserved for
    recording) and an MS-01 option.
    """

    # CRF + superfast + monochrome source matches the baseline campy/ffmpeg
    # recipe: `-c:v libx264 -crf 23 -preset superfast -pix_fmt yuv420p`.
    # Feeding GRAY8 directly lets libx264's empty-chroma compression do its
    # job; no wasted bits on a synthetic BGR upconvert.
    _PIPELINE_TEMPLATE = (
        "appsrc ! video/x-raw,format=GRAY8,width={src_w},height={src_h},framerate={fps}/1 "
        "! videoconvert ! videoscale "
        "! video/x-raw,format=I420,width={dst_w},height={dst_h} "
        "! textoverlay text={cam_label} valignment=top halignment=left "
        "font-desc=\"Sans 16\" "
        "! clockoverlay valignment=top halignment=right "
        "font-desc=\"Sans 16\" time-format=\"%Y-%m-%d %H:%M:%S\" "
        "! x264enc speed-preset={preset} tune=zerolatency "
        "pass=qual quantizer={crf} "
        "! h264parse "
        "! queue leaky=2 max-size-buffers=4 max-size-time=0 max-size-bytes=0 "
        "! rtph264pay "
        "! rtspclientsink location={mount_uri}"
    )

    def __init__(self) -> None:
        self._writer = None

    def open(self, *, src_width, src_height, fps, crf, preset, mount_uri, cam_label):
        pipeline = self._PIPELINE_TEMPLATE.format(
            src_w=src_width, src_h=src_height, fps=fps,
            dst_w=_DOWNSCALE_WIDTH, dst_h=_DOWNSCALE_HEIGHT,
            crf=crf, preset=preset,
            cam_label=cam_label,
            mount_uri=mount_uri,
        )
        self._writer = cv2.VideoWriter(
            pipeline, cv2.CAP_GSTREAMER, 0,
            float(fps), (src_width, src_height), False)
        if not self._writer.isOpened():
            raise RuntimeError("broadcast pipeline failed to open: " + pipeline)

    def write(self, frame_bgr) -> bool:
        if self._writer is None or not self._writer.isOpened():
            return False
        self._writer.write(frame_bgr)
        return self._writer.isOpened()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


def make_broadcast_backend(config: Config) -> BroadcastBackend:
    backend_name = config.broadcast.backend
    if backend_name == "libx264":
        return GStreamerLibx264Backend()
    raise ValueError("unknown broadcast.backend: " + backend_name)


class Broadcast:
    def __init__(self, context: Context, camera_config: CameraCfg,
                 broadcast_ring: FrameRing, broadcast_queue) -> None:
        self.context = context
        self.camera_config = camera_config
        self.broadcast_ring = broadcast_ring
        self.broadcast_queue = broadcast_queue
        self.logger = logging.getLogger("frameforge.broadcast")

    def run(self) -> None:
        camera_id = self.camera_config.id
        broadcast_config = self.context.config.broadcast
        mount_uri = "rtsp://%s:%d/%s" % (
            broadcast_config.rtsp_host, broadcast_config.rtsp_port, camera_id)
        self.logger.info(
            "broadcast %s starting backend=%s mount=%s",
            camera_id, broadcast_config.backend, mount_uri)

        backend = None
        try:
            backend = make_broadcast_backend(self.context.config)
            backend.open(
                src_width=self.context.config.width,
                src_height=self.context.config.height,
                fps=_BROADCAST_FPS,
                crf=broadcast_config.crf,
                preset=broadcast_config.preset,
                mount_uri=mount_uri,
                cam_label=camera_id,
            )
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 1)
            self._serve(backend, camera_id)
        except Exception:
            # Prometheus alert: bcast_session_alive == 0 for 5m
            self.logger.exception(
                "broadcast %s pipeline failed", camera_id)
            self.context.metrics.gauge(_METRIC_SESSION_ALIVE, 0)
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            self.logger.info("broadcast %s stopping", camera_id)

    def _serve(self, backend, camera_id):
        frame_count = 0
        encode_ms_window_max = 0.0
        while not self.context.drain.is_set():
            try:
                slot_index = self.broadcast_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                encode_start_mono = time.monotonic()
                backend.write(self.broadcast_ring.view(slot_index))
                encode_ms = (time.monotonic() - encode_start_mono) * 1000.0
                if encode_ms > encode_ms_window_max:
                    encode_ms_window_max = encode_ms
                frame_count += 1
                if frame_count % _SAMPLE_EVERY_N_FRAMES == 0:
                    self.context.metrics.gauge(
                        _METRIC_ENCODE_MS_MAX % camera_id, encode_ms_window_max)
                    encode_ms_window_max = 0.0
            finally:
                self.broadcast_ring.release(slot_index)
