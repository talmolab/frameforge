"""Basler cameras via pypylon: discover, open, configure, GigE tune, grab."""

import logging
import os
import time

from pypylon import genicam, pylon

from ..config import AcqCfg
from ..core.logging_setup import DEDUP_KEY
from . import Frame, SourceDisconnect

_GIGE_DEVICE_CLASS = "BaslerGigE"
_GIGE_IP_OFFSET = 100
_GIGE_NETMASK = "255.255.255.0"
_GIGE_GATEWAY = "0.0.0.0"
_PACKET_SIZE_STANDARD = 1500
_PACKET_SIZE_JUMBO = 9000
_INTER_PACKET_DELAY_NS = 0
_MAX_NUM_BUFFER = 100
_DEFAULT_RETRIEVE_TIMEOUT_MS = 3000
_FORCEIP_SETTLE_TIMEOUT_S = 6.0
_FORCEIP_POLL_INTERVAL_S = 0.5
_DEFAULT_EXPOSURE_US = 5000.0
_DEFAULT_GAIN = 0.0


class PylonSource:
    def __init__(self, camera_id: str, acq: AcqCfg, fps: float, *,
                 serial: str = "", pfs: str = "",
                 ip_override: str | None = None,
                 latest_only: bool = False) -> None:
        self.camera_id = camera_id
        self.acq = acq
        self.fps = fps
        self.serial = serial
        self.pfs = pfs
        self.ip_override = ip_override
        self.latest_only = latest_only
        self.logger = logging.getLogger("frameforge.pylon")

        self.pylon_camera = None
        self.retrieve_timeout_ms = _DEFAULT_RETRIEVE_TIMEOUT_MS
        self._pending_result = None

    def open(self) -> None:
        tl_factory = pylon.TlFactory.GetInstance()
        if self.serial:
            if self._is_gige(tl_factory):
                self._assign_ip(tl_factory)
            device_info = pylon.DeviceInfo()
            device_info.SetSerialNumber(self.serial)
            pylon_camera = pylon.InstantCamera(
                tl_factory.CreateDevice(device_info))
        else:
            pylon_camera = pylon.InstantCamera(tl_factory.CreateFirstDevice())

        try:
            pylon_camera.Open()
            self.pylon_camera = pylon_camera

            if self.pfs and os.path.isfile(self.pfs):
                pylon.FeaturePersistence.Load(
                    self.pfs, pylon_camera.GetNodeMap(), True)
                self.logger.info("cam=%s applied .pfs %s", self.camera_id, self.pfs)
            else:
                self._apply_defaults(pylon_camera)

            if pylon_camera.GetDeviceInfo().GetDeviceClass() == _GIGE_DEVICE_CLASS:
                self._apply_gige_tuning(pylon_camera)
            self.retrieve_timeout_ms = (
                _heartbeat_ms(pylon_camera) or _DEFAULT_RETRIEVE_TIMEOUT_MS)

            strategy = (pylon.GrabStrategy_LatestImageOnly if self.latest_only
                        else pylon.GrabStrategy_OneByOne)
            pylon_camera.StartGrabbing(strategy)
        except Exception:
            self.close()
            raise

        self.logger.info(
            "cam=%s open serial=%s class=%s retrieve_ms=%d",
            self.camera_id,
            pylon_camera.GetDeviceInfo().GetSerialNumber(),
            pylon_camera.GetDeviceInfo().GetDeviceClass(),
            self.retrieve_timeout_ms,
        )

    def grab(self) -> Frame | None:
        self._release_pending()
        try:
            result = self.pylon_camera.RetrieveResult(
                self.retrieve_timeout_ms, pylon.TimeoutHandling_ThrowException)
        except genicam.GenericException as pylon_error:
            raise SourceDisconnect(str(pylon_error))

        self._pending_result = result
        if not result.GrabSucceeded():
            self.logger.warning(
                "incomplete frames cam=%s code=%s msg=%s",
                self.camera_id, result.GetErrorCode(),
                result.GetErrorDescription(),
                extra={DEDUP_KEY: ("acq_incomplete", self.camera_id)})
            return None

        return Frame(result.GetArray(), time.time_ns(), result.GetBlockID())

    def close(self) -> None:
        self._release_pending()
        if self.pylon_camera is None:
            return
        try:
            self.pylon_camera.StopGrabbing()
        except Exception:
            pass
        try:
            self.pylon_camera.Close()
        except Exception:
            pass
        self.pylon_camera = None

    def _release_pending(self) -> None:
        if self._pending_result is None:
            return
        try:
            self._pending_result.Release()
        except Exception:
            pass
        self._pending_result = None

    def _is_gige(self, tl_factory) -> bool:
        for device in tl_factory.EnumerateDevices():
            if device.GetSerialNumber() == self.serial:
                return device.GetDeviceClass() == _GIGE_DEVICE_CLASS
        return True

    def _assign_ip(self, tl_factory) -> None:
        if self.ip_override:
            desired_ip = self.ip_override
        else:
            cam_number = _cam_number(self.camera_id)
            if cam_number is None:
                self.logger.warning(
                    "cam=%s id has no trailing number; skip IP assign", self.camera_id)
                return
            desired_ip = f"{self.acq.gige_subnet}.{_GIGE_IP_OFFSET + cam_number}"

        try:
            gige_tl = tl_factory.CreateTl(_GIGE_DEVICE_CLASS)
        except Exception as error:
            self.logger.warning(
                "cam=%s GigE TL unavailable err=%s", self.camera_id, error)
            return

        mac, current_ip = self._find_on_subnet(gige_tl)
        if mac is None:
            self.logger.warning(
                "cam=%s serial=%s not found on GigE subnet", self.camera_id, self.serial)
            return
        if current_ip == desired_ip:
            self.logger.debug(
                "cam=%s already at %s; skip ForceIp", self.camera_id, desired_ip)
            return

        try:
            gige_tl.ForceIp(mac, desired_ip, _GIGE_NETMASK, _GIGE_GATEWAY)
        except Exception as error:
            self.logger.warning(
                "ForceIp failed cam=%s serial=%s ip=%s err=%s",
                self.camera_id, self.serial, desired_ip, error)
            return

        self.logger.info(
            "ForceIp cam=%s serial=%s mac=%s %s->%s",
            self.camera_id, self.serial, mac, current_ip, desired_ip)

        if not self._wait_for_ip(gige_tl, desired_ip):
            self.logger.warning(
                "cam=%s not at %s within %.0fs after ForceIp "
                "(continuing; open retries)",
                self.camera_id, desired_ip, _FORCEIP_SETTLE_TIMEOUT_S)

    def _find_on_subnet(self, gige_tl):
        for device in gige_tl.EnumerateDevices():
            if device.GetSerialNumber() == self.serial:
                return device.GetMacAddress(), device.GetIpAddress()
        return None, None

    def _wait_for_ip(self, gige_tl, desired_ip) -> bool:
        deadline = time.monotonic() + _FORCEIP_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            _, current_ip = self._find_on_subnet(gige_tl)
            if current_ip == desired_ip:
                return True
            time.sleep(_FORCEIP_POLL_INTERVAL_S)
        return False

    def _apply_defaults(self, pylon_camera) -> None:
        _try_set(pylon_camera, "PixelFormat",
                 "Mono8" if self.acq.channels == 1 else "RGB8")
        _try_set(pylon_camera, "Width",  self.acq.width)
        _try_set(pylon_camera, "Height", self.acq.height)
        try:
            pylon_camera.AcquisitionFrameRateEnable.SetValue(True)
        except Exception:
            pass
        if not _try_set(pylon_camera, "AcquisitionFrameRateAbs", self.fps):
            _try_set(pylon_camera, "AcquisitionFrameRate", self.fps)

        _try_set(pylon_camera, "ExposureAuto", "Off")
        _try_set(pylon_camera, "GainAuto", "Off")
        if not _try_set(pylon_camera, "ExposureTimeAbs", _DEFAULT_EXPOSURE_US):
            _try_set(pylon_camera, "ExposureTime", _DEFAULT_EXPOSURE_US)
        if not _try_set(pylon_camera, "GainRaw", int(_DEFAULT_GAIN)):
            _try_set(pylon_camera, "Gain", _DEFAULT_GAIN)

    def _apply_gige_tuning(self, pylon_camera) -> None:
        packet_size = _PACKET_SIZE_JUMBO if self.acq.jumbo_frames else _PACKET_SIZE_STANDARD
        _try_set(pylon_camera, "GevSCPSPacketSize", packet_size)
        _try_set(pylon_camera, "GevSCPD",           _INTER_PACKET_DELAY_NS)
        try:
            pylon_camera.MaxNumBuffer.SetValue(_MAX_NUM_BUFFER)
        except Exception:
            pass


def _cam_number(cam_id: str) -> int | None:
    tail = cam_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _try_set(pylon_camera, node_name, value) -> bool:
    try:
        pylon_camera.GetNodeMap().GetNode(node_name).SetValue(value)
        return True
    except Exception:
        return False


def _heartbeat_ms(pylon_camera) -> int:
    try:
        return int(pylon_camera.GevHeartbeatTimeout.GetValue())
    except Exception:
        return 0
