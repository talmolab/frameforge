"""The one place that shells out to systemd and the journal, so Node stays testable."""

import subprocess
from collections.abc import Iterator


class SystemAdapter:
    def is_active(self, unit: str) -> str:
        result = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True)
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
        cmd = ["journalctl", "-u", unit, "-n", str(lines), "-o", "short-iso", "--no-pager"]
        if follow:
            cmd.append("-f")
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
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
        result = subprocess.run(
            ["systemctl", "--no-block", *args] if args[0] in ("start", "stop", "restart")
            else ["systemctl", *args],
            capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()
