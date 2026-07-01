"""Media backends: shared ABC + PyAV (recording) + ffmpeg subprocess (broadcast).

Recording uses PyAV to embed per-frame camera timestamps in MP4 PTS (no sidecar
needed; downstream reads timestamps via cv2.CAP_PROP_POS_MSEC).
Broadcast uses ffmpeg subprocess because hevc_qsv hardware acceleration is
significantly more ergonomic via the CLI than PyAV's libav bindings.
"""

import abc
import os
import subprocess
from fractions import Fraction

import av

from ..config import Config


class WriterDied(RuntimeError):
    pass


class MediaBackend(abc.ABC):
    @abc.abstractmethod
    def open(self, target: str, *, width: int, height: int, fps: float) -> None: ...

    @abc.abstractmethod
    def write(self, frame, ts_ns: int | None = None) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class FfmpegBackend(MediaBackend):
    _CLOSE_TIMEOUT_S = 30.0

    def __init__(self, *, codec_args, output_format=None,
                 extra_output_args=(), capture_stderr: bool = True) -> None:
        self._codec_args = list(codec_args)
        self._output_format = output_format
        self._extra_output_args = list(extra_output_args)
        self._capture_stderr = capture_stderr

        self._process = None
        self._stdin = None
        self._stderr_path = None
        self._stderr_fh = None

    def open(self, target, *, width, height, fps) -> None:
        if self._capture_stderr:
            self._stderr_path = target + ".ffmpeg.stderr"
            self._stderr_fh = open(self._stderr_path, "wb")
            stderr_dest = self._stderr_fh
        else:
            stderr_dest = subprocess.DEVNULL

        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning", "-nostats",
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "-s", "%dx%d" % (width, height),
            "-r", str(int(round(fps))),
            "-i", "-",
        ]
        cmd += self._codec_args
        if self._output_format:
            cmd += ["-f", self._output_format]
        cmd += self._extra_output_args
        cmd.append(target)

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=stderr_dest)
        self._stdin = self._process.stdin

    def write(self, frame, ts_ns: int | None = None) -> bool:
        if self._stdin is None:
            return False
        try:
            self._stdin.write(frame.tobytes())
            return True
        except (BrokenPipeError, OSError):
            return False

    def close(self) -> None:
        if self._process is None:
            return

        return_code = None
        try:
            if self._stdin is not None:
                try:
                    self._stdin.close()
                except OSError:
                    pass

            try:
                return_code = self._process.wait(timeout=self._CLOSE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._process.kill()
                return_code = self._process.wait()
        finally:
            if self._stderr_fh is not None:
                try:
                    self._stderr_fh.close()
                except OSError:
                    pass

            if return_code == 0 and self._stderr_path is not None:
                try:
                    os.remove(self._stderr_path)
                except OSError:
                    pass

            self._process = None
            self._stdin = None
            self._stderr_fh = None
            self._stderr_path = None


class PyAVBackend(MediaBackend):
    _PTS_TIME_BASE = (1, 1_000_000_000)

    def __init__(self, *, codec: str, pix_fmt: str, options: dict) -> None:
        self._codec = codec
        self._pix_fmt = pix_fmt
        self._options = dict(options)

        self._container = None
        self._stream = None
        self._chunk_start_ts_ns = None

    def open(self, target, *, width, height, fps) -> None:
        self._container = av.open(target, mode="w", format="mp4")
        self._stream = self._container.add_stream(self._codec, rate=int(round(fps)))
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = self._pix_fmt
        self._stream.time_base = Fraction(*self._PTS_TIME_BASE)
        self._stream.options = self._options

    def write(self, frame, ts_ns: int | None = None) -> bool:
        if self._stream is None:
            return False
        try:
            av_frame = av.VideoFrame.from_ndarray(frame, format="gray")
            if ts_ns is not None:
                if self._chunk_start_ts_ns is None:
                    self._chunk_start_ts_ns = ts_ns
                av_frame.pts = ts_ns - self._chunk_start_ts_ns
            for packet in self._stream.encode(av_frame):
                self._container.mux(packet)
            return True
        except av.AVError:
            return False

    def close(self) -> None:
        if self._stream is not None:
            try:
                for packet in self._stream.encode():
                    self._container.mux(packet)
            except av.AVError:
                pass
        if self._container is not None:
            try:
                self._container.close()
            except av.AVError:
                pass
        self._stream = None
        self._container = None
        self._chunk_start_ts_ns = None


def make_encoder_backend(config: Config) -> MediaBackend:
    encode = config.encode
    return FfmpegBackend(
        codec_args=[
            "-c:v", "libx264",
            "-preset", encode.preset,
            "-crf", str(encode.crf),
            "-pix_fmt", "yuv420p",
            "-g", str(encode.gop),
            "-bf", "0",
            "-movflags", "+faststart",
        ],
        output_format="mp4",
    )
