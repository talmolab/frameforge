"""The one place that shells out to systemd and the journal, so handlers stay testable.
A box without systemd reports "unknown" and failed actions rather than crashing."""

import subprocess
from collections.abc import Iterator

_TRANSIENT = ("start", "stop", "restart")


class SystemAdapter:
    def is_active(self, unit: str) -> str:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True)
        except OSError:
            return "unknown"
        return result.stdout.strip() or "unknown"

    def start(self, unit: str) -> tuple[int, str]:
        return self._ctl("start", unit)

    def stop(self, unit: str) -> tuple[int, str]:
        return self._ctl("stop", unit)

    def restart(self, unit: str) -> tuple[int, str]:
        return self._ctl("restart", unit)

    def kill(self, unit: str, sig: str = "SIGKILL") -> tuple[int, str]:
        return self._ctl("kill", "-s", sig, unit)

    def journal(self, unit: str, lines: int, follow: bool) -> Iterator[str]:
        command = ["journalctl", "-u", unit, "-n", str(lines), "-o", "short-iso", "--no-pager"]
        if follow:
            command.append("-f")
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as error:
            yield f"journalctl unavailable: {error}"
            return
        try:
            for line in process.stdout:
                yield line.rstrip("\n")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _ctl(self, *args: str) -> tuple[int, str]:
        command = ["systemctl", *(["--no-block"] if args[0] in _TRANSIENT else []), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except OSError as error:
            return 1, f"systemctl unavailable: {error}"
        return result.returncode, (result.stdout + result.stderr).strip()
