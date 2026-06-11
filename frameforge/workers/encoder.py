"""Per-camera encoder worker: drives chunk-by-chunk recording via a backend."""

import logging
import os
import queue
import time

from ..media.chunk_scheduler import ChunkScheduler
from ..core.config import CameraCfg
from ..core.context import Context
from ..media.encoder_backends import WriterDied, make_encoder_backend
from ..metrics.helpers import WindowMaxSampler
from ..metrics.defs import (
    enc_encode_duration_seconds,
    enc_encode_ms_last,
    enc_encode_ms_max,
    enc_idle,
    enc_open_failures,
    enc_writer_failures,
)
from ..core.shm_ring import FrameRing

_SAMPLE_EVERY_N_FRAMES = 50


class Encoder:
    def __init__(self, context: Context, camera_config: CameraCfg,
                 frame_ring: FrameRing, data_queue) -> None:
        self.context = context
        self.camera_config = camera_config
        self.frame_ring = frame_ring
        self.data_queue = data_queue
        self.logger = logging.getLogger("frameforge.encoder")
        self.scheduler = ChunkScheduler(
            scratch_dir=context.config.scratch_dir,
            session_name=context.session_name,
            camera_id=camera_config.id,
            chunk_seconds=context.config.encode.chunk_seconds,
        )

    def run(self) -> None:
        camera_id = self.camera_config.id
        self.logger.info(
            "encoder %s starting session=%s",
            camera_id, self.context.session_name)
        try:
            while not self.context.drain.is_set():
                chunk_index = self.scheduler.current_chunk_index()
                chunk_path = self.scheduler.chunk_path(chunk_index)
                if os.path.exists(chunk_path):
                    self._idle_until_next_chunk(chunk_index)
                else:
                    self._record_chunk(chunk_index, chunk_path)
        finally:
            self.logger.info("encoder %s stopping", camera_id)

    def _record_chunk(self, chunk_index, chunk_path):
        camera_id = self.camera_config.id
        config = self.context.config
        partial_chunk_path = chunk_path + ".part"
        os.makedirs(os.path.dirname(partial_chunk_path), exist_ok=True)

        backend = make_encoder_backend(config)
        target_frames = self.scheduler.target_frames(config.encode.fps)
        opened_index = chunk_index
        try:
            backend.open(
                partial_chunk_path,
                width=config.width, height=config.height,
                fps=config.encode.fps,
            )
        except Exception:
            enc_open_failures.labels(cam=camera_id).inc()
            self.logger.exception(
                "backend.open failed cam=%s index=%d",
                camera_id, chunk_index)
            raise

        metric_encode_hist = enc_encode_duration_seconds.labels(cam=camera_id)
        encode_ms_sampler = WindowMaxSampler(
            every=_SAMPLE_EVERY_N_FRAMES,
            gauge_last=enc_encode_ms_last.labels(cam=camera_id),
            gauge_max=enc_encode_ms_max.labels(cam=camera_id),
        )

        self.logger.info(
            "opened chunk cam=%s index=%d target=%d path=%s",
            camera_id, chunk_index, target_frames, partial_chunk_path)

        frames_written = 0
        try:
            while frames_written < target_frames and not self.context.hard_drain.is_set():
                if self.scheduler.current_chunk_index() != opened_index:
                    break

                try:
                    slot_index = self.data_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                try:
                    encode_start_mono = time.monotonic()
                    if not backend.write(self.frame_ring.view(slot_index)):
                        raise WriterDied("backend.write returned False")

                    encode_seconds = time.monotonic() - encode_start_mono
                    metric_encode_hist.observe(encode_seconds)
                    encode_ms_sampler.observe(encode_seconds * 1000.0)

                    frames_written += 1
                finally:
                    self.frame_ring.release(slot_index)

        except WriterDied as writer_error:
            self.logger.error(
                "WRITER DIED cam=%s index=%d frame=%d/%d err=%s",
                camera_id, chunk_index, frames_written, target_frames,
                writer_error)
            enc_writer_failures.labels(cam=camera_id).inc()
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
        metric_idle = enc_idle.labels(cam=camera_id)

        self.logger.info(
            "encoder idle cam=%s index=%d file exists, waiting for next chunk",
            camera_id, chunk_index)
        metric_idle.set(1)

        while (not self.context.drain.is_set()
               and self.scheduler.current_chunk_index() == chunk_index):
            try:
                slot_index = self.data_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.frame_ring.release(slot_index)

        metric_idle.set(0)
        self.logger.info("encoder resumed cam=%s", camera_id)
