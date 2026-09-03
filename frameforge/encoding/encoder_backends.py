"""Media backends: shared ABC + ffmpeg subprocess for both recording and broadcast."""

import abc
import os
import subprocess

from ..config import Config


class WriterDied(RuntimeError):
    pass


_INPUT_PIX_FMT = {1: "gray", 3: "rgb24"}


class MediaBackend(abc.ABC):
    @abc.abstractmethod
    def open(self, target: str, *, width: int, height: int, fps: float,
             channels: int = 1) -> None: ...

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

    def open(self, target, *, width, height, fps, channels=1) -> None:
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
            "-pix_fmt", _INPUT_PIX_FMT[channels],
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
