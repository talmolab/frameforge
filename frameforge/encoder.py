"""Per-camera HW encoder.

- Chunks are wall-clock-hour-aligned (matches metadata_helper math).
- Each outer-loop iteration derives ``chunk_index`` from the wall clock and
  either records this hour's chunk OR — if the file already exists for this
  hour — drains the ring/queue until the next hour. The discard branch is how
  we recover from a mid-chunk writer failure: the partial was finalized and
  renamed, so this hour is done.
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
_METRIC_SYNTHETIC = "enc.%s.synthetic"
_METRIC_WRITER_FAILURES = "enc.%s.writer_failures"
_METRIC_OPEN_FAILURES = "enc.%s.open_failures"
_METRIC_DISCARDED = "enc.%s.discarded"
_METRIC_ENCODE_MS_LAST = "enc.%s.encode_ms_last"
_METRIC_ENCODE_MS_MAX = "enc.%s.encode_ms_max"

_SAMPLE_EVERY_N_FRAMES = 50


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
            "encoder %s starting (session=%s recstart=%s)",
            camera_id,
            self.context.session_name,
            self.context.recording_start_str)
        try:
            while not self.context.drain.is_set():
                chunk_index = self._current_chunk_index()
                chunk_path = self._chunk_path(chunk_index)
                if os.path.exists(chunk_path):
                    self._discard_until_next_chunk(chunk_index)
                else:
                    self._record_chunk(chunk_index, chunk_path)
        finally:
            self.logger.info("encoder %s stopping", camera_id)

    def _current_chunk_index(self):
        elapsed = datetime.datetime.now() - self.context.recording_start
        return max(0, int(elapsed.total_seconds() // 3600))

    def _chunk_path(self, chunk_index):
        return os.path.join(
            self.context.config.paths.scratch,
            self.context.session_name,
            self.camera_config.id,
            self.context.recording_start_str,
            "%s.%02d.mp4" % (self.camera_config.id, chunk_index),
        )

    def _frames_for_chunk(self, _chunk_index):
        return int(round(3600.0 * self.context.config.encode.fps))

    def _chunk_end(self, chunk_index):
        return (self.context.recording_start
                + datetime.timedelta(hours=chunk_index + 1))

    def _record_chunk(self, chunk_index, chunk_path):
        camera_id = self.camera_config.id
        config = self.context.config
        partial_chunk_path = chunk_path + ".part"
        os.makedirs(os.path.dirname(partial_chunk_path), exist_ok=True)

        backend = make_backend(config)
        target_frames = self._frames_for_chunk(chunk_index)
        chunk_end = self._chunk_end(chunk_index)
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
                "backend.open failed cam=%s chunk=%d -> raising",
                camera_id, chunk_index)
            raise

        self.logger.info(
            "opened chunk cam=%s idx=%d target=%d -> %s",
            camera_id, chunk_index, target_frames, partial_chunk_path)

        frames_written = 0
        encode_ms_window_max = 0.0
        try:
            while frames_written < target_frames and not self.context.drain.is_set():
                if datetime.datetime.now() >= chunk_end:
                    break
                try:
                    slot_index, hardware_timestamp, _host_monotonic_ns = (
                        self.data_queue.get(timeout=1.0))
                except queue.Empty:
                    continue

                try:
                    if hardware_timestamp == 0:
                        self.context.metrics.incr(_METRIC_SYNTHETIC % camera_id)
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
            # and enters discard mode for the rest of the hour.
            self.logger.error(
                "WRITER DIED cam=%s chunk=%d at frame=%d/%d: %s",
                camera_id, chunk_index, frames_written, target_frames,
                writer_error)
            self.context.metrics.incr(_METRIC_WRITER_FAILURES % camera_id)
        finally:
            try:
                backend.close()
            except Exception:
                self.logger.exception(
                    "backend.close failed cam=%s chunk=%d",
                    camera_id, chunk_index)
            if os.path.exists(partial_chunk_path):
                try:
                    os.rename(partial_chunk_path, chunk_path)
                    self.logger.info(
                        "FINALIZED %s frames=%d/%d",
                        chunk_path, frames_written, target_frames)
                except OSError:
                    self.logger.exception(
                        "rename failed %s -> %s",
                        partial_chunk_path, chunk_path)

    def _discard_until_next_chunk(self, chunk_index):
        camera_id = self.camera_config.id
        deadline = (
            self.context.recording_start
            + datetime.timedelta(hours=chunk_index + 1))
        self.logger.warning(
            "DISCARD MODE cam=%s chunk=%d already exists; waiting until %s",
            camera_id, chunk_index, deadline)

        while not self.context.drain.is_set() and datetime.datetime.now() < deadline:
            try:
                slot_index, _hw, _host = self.data_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.frame_ring.release(slot_index)
            self.context.metrics.incr(_METRIC_DISCARDED % camera_id)
