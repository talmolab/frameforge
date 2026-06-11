"""Per-camera live broadcast worker: GRAY8 frames pushed to RTSP via ffmpeg."""

import logging
import queue
import time

from ..core.config import CameraCfg, Config
from ..core.context import Context
from ..media.encoder_backends import FfmpegBackend, MediaBackend
from ..metrics.helpers import WindowMaxSampler
from ..metrics.defs import (
    bcast_encode_duration_seconds,
    bcast_encode_ms_last,
    bcast_encode_ms_max,
    bcast_session_alive,
)
from ..core.shm_ring import FrameRing

_BROADCAST_FPS = 10
_SAMPLE_EVERY_N_FRAMES = 50


def make_broadcast_backend(config: Config) -> MediaBackend:
    bcast = config.broadcast
    if bcast.backend == "libx264":
        return FfmpegBackend(
            codec_args=[
                "-c:v", "libx264",
                "-preset", bcast.preset,
                "-crf", str(bcast.crf),
                "-pix_fmt", "yuv420p",
                "-tune", "zerolatency",
            ],
            output_format="rtsp",
            extra_output_args=("-rtsp_transport", "tcp"),
            capture_stderr=False,
        )
    raise ValueError("unknown broadcast.backend: " + bcast.backend)


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
                mount_uri,
                width=self.context.config.width,
                height=self.context.config.height,
                fps=_BROADCAST_FPS,
            )
            bcast_session_alive.labels(cam=camera_id).set(1)
            self._serve(backend, camera_id)
        except Exception:
            self.logger.exception(
                "broadcast %s pipeline failed", camera_id)
            bcast_session_alive.labels(cam=camera_id).set(0)
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            self.logger.info("broadcast %s stopping", camera_id)

    def _serve(self, backend, camera_id):
        metric_encode_hist = bcast_encode_duration_seconds.labels(cam=camera_id)
        encode_ms_sampler = WindowMaxSampler(
            every=_SAMPLE_EVERY_N_FRAMES,
            gauge_last=bcast_encode_ms_last.labels(cam=camera_id),
            gauge_max=bcast_encode_ms_max.labels(cam=camera_id),
        )

        while not self.context.drain.is_set():
            try:
                slot_index = self.broadcast_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                encode_start_mono = time.monotonic()
                backend.write(self.broadcast_ring.view(slot_index))

                encode_seconds = time.monotonic() - encode_start_mono
                metric_encode_hist.observe(encode_seconds)
                encode_ms_sampler.observe(encode_seconds * 1000.0)
            finally:
                self.broadcast_ring.release(slot_index)
