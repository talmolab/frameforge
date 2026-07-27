import os
import subprocess
import sys
import time

import cv2
import numpy as np
from pypylon import pylon

from frameforge.config import CameraCfg, load_config
from frameforge.media.camera import Camera

os.environ.setdefault("FF_HARDWARE", "ms01")


FOCUS_MIN = 100.0
P5_MIN = 5.0
P95_MAX = 250.0
P50_RANGE = (90.0, 160.0)
CHARUCO_TARGET = 55

RTSP_URL = "rtsp://127.0.0.1:8554/test_cam"
BROADCAST_FPS = 10
CHARUCO_EVERY = 15
CALIB_IP = "192.168.10.200"


def build_charuco_detector():
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_1000)
    board = cv2.aruco.CharucoBoard([6, 12], 24.0, 18.75, aruco_dict)

    parameters = cv2.aruco.DetectorParameters()
    parameters.adaptiveThreshWinSizeMin = 5
    parameters.adaptiveThreshWinSizeMax = 23
    parameters.adaptiveThreshWinSizeStep = 10
    parameters.adaptiveThreshConstant = 7
    parameters.minMarkerPerimeterRate = 0.03
    parameters.maxMarkerPerimeterRate = 4.0
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    parameters.minOtsuStdDev = 5.0

    return cv2.aruco.CharucoDetector(board, detectorParams=parameters)


def detect_charuco_board(detector, img):
    charuco_corners, _, _, _ = detector.detectBoard(img)

    if charuco_corners is not None and len(charuco_corners) > 0:
        points = charuco_corners.reshape(-1, 2)
        return points[0], points[-1], len(points), True

    return None, None, 0, False


class Broadcaster:
    def __init__(self, width, height):
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "gray",
             "-s", f"{width}x{height}", "-r", str(BROADCAST_FPS), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
             "-profile:v", "baseline", "-g", "20", "-pix_fmt", "yuv420p",
             "-f", "rtsp", "-rtsp_transport", "tcp", RTSP_URL],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def send(self, frame):
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            self.close()

    def close(self):
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None


def _make_broadcaster(width, height):
    try:
        return Broadcaster(width, height)
    except Exception as error:
        print(f"[broadcast disabled: {error}]")
        return None


def main(serial_number, test_ip):
    config = load_config()
    camera_config = CameraCfg(id="calib", serial=serial_number)
    camera = Camera(camera_config, config, ip_override=test_ip)
    camera.open()
    pylon_camera = camera.pylon_camera
    pylon_camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    broadcaster = _make_broadcaster(
        pylon_camera.Width.GetValue(), pylon_camera.Height.GetValue())

    print(f"camera {serial_number} @ {test_ip} — live calibration readout (Ctrl-C to stop)")
    print(f"targets: focus>={FOCUS_MIN:.0f}  p5>={P5_MIN:.0f}  p95<={P95_MAX:.0f}  "
          f"p50 in [{P50_RANGE[0]:.0f},{P50_RANGE[1]:.0f}]  charuco={CHARUCO_TARGET}")
    if broadcaster:
        print(f"broadcast: http://{os.uname().nodename}:8888/test_cam")

    detector = build_charuco_detector()
    frame_index = 0
    last_broadcast = 0.0
    top_left = bottom_right = None
    num_corners = 0
    board_detected = False
    try:
        while pylon_camera.IsGrabbing():
            grab_result = pylon_camera.RetrieveResult(
                5000, pylon.TimeoutHandling_ThrowException)

            if grab_result.GrabSucceeded():
                frame = grab_result.GetArray()
                gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

                focus = cv2.Laplacian(gray, cv2.CV_64F).var()
                prc5, prc50, prc95 = np.percentile(gray, [5, 50, 95])
                if frame_index % CHARUCO_EVERY == 0:
                    top_left, bottom_right, num_corners, board_detected = \
                        detect_charuco_board(detector, gray)

                focus_mark = "OK" if focus >= FOCUS_MIN else ".."
                exp_flags = []
                if prc5 < P5_MIN:
                    exp_flags.append("dark")
                if prc95 > P95_MAX:
                    exp_flags.append("clip")
                if not P50_RANGE[0] <= prc50 <= P50_RANGE[1]:
                    exp_flags.append("mid")
                exp_mark = "OK" if not exp_flags else "..(" + ",".join(exp_flags) + ")"
                char_mark = "OK" if num_corners >= CHARUCO_TARGET else ".."

                readout = (
                    f"focus={focus:7.1f} {focus_mark}  "
                    f"illum {prc5:3.0f}/{prc50:3.0f}/{prc95:3.0f} {exp_mark}  "
                    f"charuco={num_corners:>2}/{CHARUCO_TARGET} {char_mark}")

                frame_str = ""
                if board_detected:
                    frame_str = (
                        f"  TL=({int(top_left[0])},{int(top_left[1])})"
                        f" BR=({int(bottom_right[0])},{int(bottom_right[1])})")

                sys.stdout.write(f"\r{readout}{frame_str}\033[K")
                sys.stdout.flush()

                now = time.monotonic()
                if broadcaster and now - last_broadcast >= 1.0 / BROADCAST_FPS:
                    overlay = gray.copy()
                    cv2.putText(overlay, readout, (12, 34),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, 255, 2)
                    broadcaster.send(overlay)
                    last_broadcast = now
                frame_index += 1

            grab_result.Release()
    except KeyboardInterrupt:
        print()
    finally:
        if broadcaster:
            broadcaster.close()
        try:
            pylon_camera.StopGrabbing()
        except Exception:
            pass
        camera.close()


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 3:
        sys.exit(f"usage: calibrator.py <serial> [test_ip]  (default {CALIB_IP})")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else CALIB_IP)
