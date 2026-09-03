"""Per-camera encoder worker: drives chunk-by-chunk recording via a backend."""

import logging
import os
import queue
import time

import h5py
import numpy as np

from ..encoding.chunk_scheduler import ChunkScheduler, sidecar_for
from ..context import Context
from ..core.paths import SCRATCH_DIR
from ..encoding.ffmpeg import WriterDied, make_encoder_backend
from ..metrics.defs import (
    enc_encode_duration_seconds,
    enc_idle,
    enc_open_failures,
    enc_writer_failures,
)
from ..core.shm_ring import FrameRing

_QUEUE_GET_TIMEOUT_S = 1.0


class Encoder:
    def __init__(self, context: Context, camera_id: str,
                 frame_ring: FrameRing, data_queue) -> None:
        self.context = context
        self.camera_id = camera_id
        self.frame_ring = frame_ring
        self.data_queue = data_queue
        self.logger = logging.getLogger("frameforge.encoder")
        self.scheduler = ChunkScheduler(
            scratch_dir=SCRATCH_DIR,
            session_name=context.session_name,
            camera_id=camera_id,
            chunk_seconds=context.config.encode.chunk_seconds,
            timezone=context.config.encode.timezone,
        )

    def run(self) -> None:
        camera_id = self.camera_id
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
        camera_id = self.camera_id
        config = self.context.config
        partial_chunk_path = chunk_path + ".part"
        os.makedirs(os.path.dirname(partial_chunk_path), exist_ok=True)

        backend = make_encoder_backend(config.encode)
        target_frames = self.scheduler.target_frames(config.encode.fps)
        opened_index = chunk_index
        try:
            backend.open(
                partial_chunk_path,
                width=config.acq.width, height=config.acq.height,
                fps=config.encode.fps, channels=config.acq.channels,
            )
        except Exception:
            enc_open_failures.labels(cam=camera_id).inc()
            self.logger.exception(
                "backend.open failed cam=%s index=%d",
                camera_id, chunk_index)
            raise

        metric_encode_hist = enc_encode_duration_seconds.labels(cam=camera_id)

        self.logger.info(
            "opened chunk cam=%s index=%d target=%d path=%s",
            camera_id, chunk_index, target_frames, partial_chunk_path)

        frames_written = 0
        chunk_timestamps: list[int] = []
        try:
            while frames_written < target_frames and not self.context.hard_drain.is_set():
                if self.scheduler.current_chunk_index() != opened_index:
                    break

                try:
                    slot_index, ts_ns = self.data_queue.get(
                        timeout=_QUEUE_GET_TIMEOUT_S)
                except queue.Empty:
                    continue

                try:
                    encode_start_ns = time.monotonic_ns()
                    if not backend.write(
                            self.frame_ring.view(slot_index), ts_ns=ts_ns):
                        raise WriterDied("backend.write returned False")

                    encode_ns = time.monotonic_ns() - encode_start_ns
                    metric_encode_hist.observe(encode_ns / 1_000_000_000.0)

                    chunk_timestamps.append(ts_ns)
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

            if self.context.hard_drain.is_set():
                self.logger.info(
                    "chunk aborted cam=%s index=%d frames=%d/%d (hard drain)",
                    camera_id, chunk_index, frames_written, target_frames)
            else:
                self._write_sidecar(chunk_path, chunk_timestamps)
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

    def _write_sidecar(self, chunk_path: str, timestamps: list[int]) -> None:
        if not timestamps:
            return
        sidecar_path = sidecar_for(chunk_path)
        partial = sidecar_path + ".part"
        try:
            with h5py.File(partial, "w") as h5:
                h5.create_dataset(
                    "timestamps",
                    data=np.array(timestamps, dtype=np.int64))
                h5.attrs["fps"] = float(self.context.config.encode.fps)
                h5.attrs["host"] = os.uname().nodename
            os.rename(partial, sidecar_path)
        except OSError:
            self.logger.exception(
                "sidecar write failed cam=%s path=%s",
                self.camera_id, sidecar_path)
            try:
                os.remove(partial)
            except OSError:
                pass

    def _idle_until_next_chunk(self, chunk_index):
        camera_id = self.camera_id
        metric_idle = enc_idle.labels(cam=camera_id)

        self.logger.info(
            "encoder idle cam=%s index=%d file exists, waiting for next chunk",
            camera_id, chunk_index)
        metric_idle.set(1)

        while (not self.context.drain.is_set()
               and self.scheduler.current_chunk_index() == chunk_index):
            try:
                slot_index, _ = self.data_queue.get(timeout=_QUEUE_GET_TIMEOUT_S)
            except queue.Empty:
                continue
            self.frame_ring.release(slot_index)

        metric_idle.set(0)
        self.logger.info("encoder resumed cam=%s", camera_id)
