"""Per-camera live broadcast worker: GRAY8 frames pushed to RTSP via ffmpeg."""

import logging
import queue
import time

from ..config import Config
from ..context import Context
from ..media.encoder_backends import FfmpegBackend, MediaBackend
from ..metrics.defs import bcast_encode_duration_seconds, broadcast_enabled
from ..core.shm_ring import FrameRing

_BROADCAST_FPS = 10
_OPEN_MAX_ATTEMPTS = 5
_OPEN_BACKOFF_S = 2.0
_RTSP_HOST = "127.0.0.1"
_RTSP_PORT = 8554


def make_broadcast_backend(config: Config) -> MediaBackend:
    bcast = config.broadcast
    return FfmpegBackend(
        codec_args=[
            "-c:v", "h264_qsv",
            "-preset", "veryfast",
            "-profile:v", "baseline",
            "-b:v", str(int(bcast.bitrate_mbps * 1_000_000)),
            "-look_ahead", "0",
            "-g", "20",
            "-bf", "0",
            "-pix_fmt", "nv12",
        ],
        output_format="rtsp",
        extra_output_args=("-rtsp_transport", "tcp"),
        capture_stderr=False,
    )


class Broadcast:
    def __init__(self, context: Context, camera_id: str,
                 broadcast_ring: FrameRing, broadcast_queue) -> None:
        self.context = context
        self.camera_id = camera_id
        self.broadcast_ring = broadcast_ring
        self.broadcast_queue = broadcast_queue
        self.logger = logging.getLogger("frameforge.broadcast")

    def run(self) -> None:
        camera_id = self.camera_id
        mount_uri = "rtsp://%s:%d/%s" % (_RTSP_HOST, _RTSP_PORT, camera_id)

        self.logger.info("broadcast starting mount=%s", mount_uri)

        backend = self._open_with_retry(mount_uri, camera_id)
        if backend is None:
            broadcast_enabled.set(0)
            self.logger.error(
                "broadcast %s giving up after %d open attempts",
                camera_id, _OPEN_MAX_ATTEMPTS)
            return

        broadcast_enabled.set(1)
        try:
            self._serve(backend, camera_id)
        except Exception:
            self.logger.exception(
                "broadcast %s serve failed", camera_id)
        finally:
            try:
                backend.close()
            except Exception:
                pass
            broadcast_enabled.set(0)
            self.logger.info("broadcast %s stopping", camera_id)

    def _open_with_retry(self, mount_uri: str, camera_id: str):
        for attempt in range(_OPEN_MAX_ATTEMPTS):
            if self.context.hard_drain.is_set():
                return None
            try:
                backend = make_broadcast_backend(self.context.config)
                backend.open(
                    mount_uri,
                    width=self.context.config.acq.width,
                    height=self.context.config.acq.height,
                    fps=_BROADCAST_FPS,
                )
                return backend
            except Exception:
                self.logger.exception(
                    "broadcast %s open failed attempt=%d/%d",
                    camera_id, attempt + 1, _OPEN_MAX_ATTEMPTS)
                if attempt + 1 < _OPEN_MAX_ATTEMPTS:
                    if self.context.hard_drain.wait(_OPEN_BACKOFF_S * (attempt + 1)):
                        return None
        return None

    def _serve(self, backend, camera_id):
        metric_encode_hist = bcast_encode_duration_seconds.labels(cam=camera_id)

        while not self.context.hard_drain.is_set():
            try:
                slot_index = self.broadcast_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                encode_start_ns = time.monotonic_ns()
                backend.write(self.broadcast_ring.view(slot_index))

                encode_ns = time.monotonic_ns() - encode_start_ns
                metric_encode_hist.observe(encode_ns / 1_000_000_000.0)
            finally:
                self.broadcast_ring.release(slot_index)
