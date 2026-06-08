"""Per-camera HW encoder.

- ``chunk_index`` = TZ-aware elapsed hours since today's midnight (in local
  TZ). Normal days produce 24 chunks (0-23 matching wall-clock hours);
  DST spring-forward produces 23 (0-22, no gap); DST fall-back produces 25
  (0-24, no collision). Mid-day startup naturally lands at the current
  elapsed hour.
- Each outer-loop iteration computes ``chunk_index`` and either records this
  chunk or — if the .mp4 already exists (crash recovery, same-hour restart) —
  enters idle mode until the next chunk boundary. Idle drains the ring/queue
  without writing so acq isn't artificially back-pressured.
- Backend is a small ABC + factory so x86/QuickSync drops in without touching
  the orchestration above.
"""

import abc
import datetime
import logging
import os
import queue
import time

import cv2

from .config import CameraCfg, Config
from .context import Context
from .shm_ring import FrameRing


_METRIC_FRAMES = "enc.%s.frames"
_METRIC_WRITER_FAILURES = "enc.%s.writer_failures"
_METRIC_OPEN_FAILURES = "enc.%s.open_failures"
_METRIC_IDLE = "enc.%s.idle"
_METRIC_DRAIN_PENDING = "enc.%s.drain_pending"
_METRIC_ENCODE_MS_LAST = "enc.%s.encode_ms_last"
_METRIC_ENCODE_MS_MAX = "enc.%s.encode_ms_max"

_SAMPLE_EVERY_N_FRAMES = 50
_SOFT_DRAIN_LOG_INTERVAL_S = 60.0


class WriterDied(RuntimeError):
    pass


class EncoderBackend(abc.ABC):
    @abc.abstractmethod
    def open(self, path: str, *, width: int, height: int,
             fps: float, bitrate_bps: int, gop: int) -> None: ...

    @abc.abstractmethod
    def write(self, frame_bgr) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class GStreamerNvencBackend(EncoderBackend):
    """Jetson HW H.264/H.265 via cv2.VideoWriter wrapping a GStreamer pipeline.

    Same NVENC silicon; only the encoder element + parser differ between codecs.
    """

    _CODEC_ELEMENTS = {
        "h264": ("nvv4l2h264enc", "h264parse"),
        "h265": ("nvv4l2h265enc", "h265parse"),
    }
    _PIPELINE_TEMPLATE = (
        "appsrc ! video/x-raw,format=BGR ! videoconvert "
        "! video/x-raw,format=BGRx ! nvvidconv "
        "! {encoder} bitrate={bitrate} control-rate=1 "
        "iframeinterval={gop} insert-sps-pps=1 maxperf-enable=1 "
        "! {parser} ! qtmux faststart=true ! filesink location={path}"
    )

    def __init__(self, codec: str = "h265") -> None:
        if codec not in self._CODEC_ELEMENTS:
            raise ValueError("unsupported codec: " + codec)
        self._encoder_element, self._parser_element = self._CODEC_ELEMENTS[codec]
        self._writer = None

    def open(self, path, *, width, height, fps, bitrate_bps, gop) -> None:
        pipeline = self._PIPELINE_TEMPLATE.format(
            encoder=self._encoder_element,
            parser=self._parser_element,
            bitrate=bitrate_bps, gop=gop, path=path,
        )
        self._writer = cv2.VideoWriter(
            pipeline, cv2.CAP_GSTREAMER, 0,
            float(fps), (width, height), True)
        if not self._writer.isOpened():
            raise RuntimeError("GStreamer pipeline failed to open: " + pipeline)

    def write(self, frame_bgr) -> bool:
        if self._writer is None or not self._writer.isOpened():
            return False
        self._writer.write(frame_bgr)
        return self._writer.isOpened()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


def make_backend(config: Config) -> EncoderBackend:
    backend_name = config.encode.backend
    if backend_name == "nvv4l2h264enc":
        return GStreamerNvencBackend(codec="h264")
    if backend_name == "nvv4l2h265enc":
        return GStreamerNvencBackend(codec="h265")
    raise ValueError("unknown encode.backend: " + backend_name)


class Encoder:
    def __init__(self, context: Context, camera_config: CameraCfg,
                 frame_ring: FrameRing, data_queue) -> None:
        self.context = context
        self.camera_config = camera_config
        self.frame_ring = frame_ring
        self.data_queue = data_queue
        self.logger = logging.getLogger("frameforge.encoder")

    def run(self) -> None:
        camera_id = self.camera_config.id
        self.logger.info(
            "encoder %s starting session=%s",
            camera_id, self.context.session_name)
        try:
            while not self.context.drain.is_set():
                chunk_index = self._current_chunk_index()
                recording_start_str = self._today_midnight_str()
                chunk_path = self._chunk_path(recording_start_str, chunk_index)
                if os.path.exists(chunk_path):
                    self._idle_until_next_chunk(chunk_index)
                else:
                    self._record_chunk(chunk_index, chunk_path)
        finally:
            self.logger.info("encoder %s stopping", camera_id)

    def _today_midnight_aware(self):
        now = datetime.datetime.now().astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _today_midnight_str(self):
        return self._today_midnight_aware().strftime("%Y-%m-%d-%H-%M-%S")

    def _current_chunk_index(self):
        elapsed_s = (datetime.datetime.now().astimezone()
                     - self._today_midnight_aware()).total_seconds()
        return max(0, int(elapsed_s // 3600))

    def _chunk_path(self, recording_start_str, chunk_index):
        return os.path.join(
            self.context.config.paths.scratch,
            self.context.session_name,
            self.camera_config.id,
            recording_start_str,
            "%s.%02d.mp4" % (self.camera_config.id, chunk_index),
        )

    def _target_frames(self):
        return int(round(3600.0 * self.context.config.encode.fps))

    def _record_chunk(self, chunk_index, chunk_path):
        camera_id = self.camera_config.id
        config = self.context.config
        partial_chunk_path = chunk_path + ".part"
        os.makedirs(os.path.dirname(partial_chunk_path), exist_ok=True)

        backend = make_backend(config)
        target_frames = self._target_frames()
        opened_index = chunk_index
        try:
            backend.open(
                partial_chunk_path,
                width=config.width, height=config.height,
                fps=config.encode.fps,
                bitrate_bps=int(config.encode.bitrate_mbps * 1_000_000),
                gop=config.encode.gop,
            )
        except Exception:
            # Open failure = severe (backend probably unusable). Raise so the
            # supervisor restarts with exponential backoff, avoiding a tight
            # retry loop. Write failure mid-chunk is handled below.
            self.context.metrics.incr(_METRIC_OPEN_FAILURES % camera_id)
            self.logger.exception(
                "backend.open failed cam=%s index=%d",
                camera_id, chunk_index)
            raise

        self.context.metrics.gauge(_METRIC_DRAIN_PENDING % camera_id, 0)
        self.logger.info(
            "opened chunk cam=%s index=%d target=%d path=%s",
            camera_id, chunk_index, target_frames, partial_chunk_path)

        frames_written = 0
        encode_ms_window_max = 0.0
        last_soft_drain_log_at = 0.0
        fps = config.encode.fps
        try:
            while frames_written < target_frames and not self.context.hard_drain.is_set():
                if self._current_chunk_index() != opened_index:
                    break
                if self.context.drain.is_set():
                    self.context.metrics.gauge(
                        _METRIC_DRAIN_PENDING % camera_id, 1)
                    now_mono = time.monotonic()
                    if now_mono - last_soft_drain_log_at >= _SOFT_DRAIN_LOG_INTERVAL_S:
                        eta_s = int(max(0, (target_frames - frames_written) / fps))
                        self.logger.info(
                            "soft drain pending cam=%s frames=%d/%d eta_s=%d",
                            camera_id, frames_written, target_frames, eta_s)
                        last_soft_drain_log_at = now_mono
                try:
                    slot_index = self.data_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                try:
                    encode_start_mono = time.monotonic()
                    bgr_frame = cv2.cvtColor(
                        self.frame_ring.view(slot_index),
                        cv2.COLOR_GRAY2BGR)
                    if not backend.write(bgr_frame):
                        raise WriterDied("backend.write returned False")
                    encode_ms = (time.monotonic() - encode_start_mono) * 1000.0
                    if encode_ms > encode_ms_window_max:
                        encode_ms_window_max = encode_ms
                    frames_written += 1
                    self.context.metrics.incr(_METRIC_FRAMES % camera_id)
                    if frames_written % _SAMPLE_EVERY_N_FRAMES == 0:
                        self.context.metrics.gauge(
                            _METRIC_ENCODE_MS_LAST % camera_id, encode_ms)
                        self.context.metrics.gauge(
                            _METRIC_ENCODE_MS_MAX % camera_id, encode_ms_window_max)
                        encode_ms_window_max = 0.0
                finally:
                    self.frame_ring.release(slot_index)

        except WriterDied as writer_error:
            # Fail loud, keep the partial. close() flushes moov so what's
            # captured is playable; outer loop sees the .mp4 next iteration
            # and enters idle mode until the next chunk boundary.
            # Prometheus alert: enc_writer_failures_total rate > 0
            self.logger.error(
                "WRITER DIED cam=%s index=%d frame=%d/%d err=%s",
                camera_id, chunk_index, frames_written, target_frames,
                writer_error)
            self.context.metrics.incr(_METRIC_WRITER_FAILURES % camera_id)
        finally:
            try:
                backend.close()
            except Exception:
                self.logger.exception(
                    "backend.close failed cam=%s index=%d",
                    camera_id, chunk_index)
            if os.path.exists(partial_chunk_path):
                try:
                    os.rename(partial_chunk_path, chunk_path)
                    self.logger.info(
                        "finalized path=%s frames=%d/%d",
                        chunk_path, frames_written, target_frames)
                except OSError:
                    self.logger.exception(
                        "rename failed src=%s dst=%s",
                        partial_chunk_path, chunk_path)

    def _idle_until_next_chunk(self, chunk_index):
        camera_id = self.camera_config.id
        self.logger.info(
            "encoder idle cam=%s index=%d file exists, waiting for next chunk",
            camera_id, chunk_index)
        self.context.metrics.gauge(_METRIC_IDLE % camera_id, 1)

        while (not self.context.drain.is_set()
               and self._current_chunk_index() == chunk_index):
            try:
                slot_index = self.data_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.frame_ring.release(slot_index)

        self.context.metrics.gauge(_METRIC_IDLE % camera_id, 0)
        self.logger.info("encoder resumed cam=%s", camera_id)
