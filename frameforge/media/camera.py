"""Basler camera lifecycle: discover, open, configure, GigE tune."""

import logging
import os
import time

from pypylon import pylon

from ..config import CameraCfg, Config
from ..metrics.defs import acq_camera_alive

_GIGE_SUBNET_PREFIX = "192.168.10"
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


class Camera:
    def __init__(self, camera_config: CameraCfg, full_config: Config,
                 ip_override: str | None = None) -> None:
        self.camera_config = camera_config
        self.config = full_config
        self.ip_override = ip_override
        self.logger = logging.getLogger("frameforge.camera")

        self.pylon_camera = None
        self.retrieve_timeout_ms = _DEFAULT_RETRIEVE_TIMEOUT_MS

    def open(self) -> None:
        tl_factory = pylon.TlFactory.GetInstance()
        if self.camera_config.serial:
            self._assign_ip(tl_factory)
            device_info = pylon.DeviceInfo()
            device_info.SetSerialNumber(self.camera_config.serial)
            pylon_camera = pylon.InstantCamera(
                tl_factory.CreateDevice(device_info))
        else:
            pylon_camera = pylon.InstantCamera(tl_factory.CreateFirstDevice())

        try:
            pylon_camera.Open()
            self.pylon_camera = pylon_camera

            if self.camera_config.pfs and os.path.isfile(self.camera_config.pfs):
                pylon.FeaturePersistence.Load(
                    self.camera_config.pfs, pylon_camera.GetNodeMap(), True)
                self.logger.info("cam=%s applied .pfs %s",
                                 self.camera_config.id, self.camera_config.pfs)
            else:
                self._apply_defaults(pylon_camera)

            self._apply_gige_tuning(pylon_camera)
            self.retrieve_timeout_ms = (
                _heartbeat_ms(pylon_camera) or _DEFAULT_RETRIEVE_TIMEOUT_MS)
        except Exception:
            acq_camera_alive.labels(cam=self.camera_config.id).set(0)
            self.close()
            raise

        acq_camera_alive.labels(cam=self.camera_config.id).set(1)

        self.logger.info(
            "cam=%s open serial=%s retrieve_ms=%d",
            self.camera_config.id,
            pylon_camera.GetDeviceInfo().GetSerialNumber(),
            self.retrieve_timeout_ms,
        )

    def close(self) -> None:
        if self.pylon_camera is None:
            return
        try:
            self.pylon_camera.Close()
        except Exception:
            pass
        self.pylon_camera = None
        acq_camera_alive.labels(cam=self.camera_config.id).set(0)

    def _assign_ip(self, tl_factory) -> None:
        cfg = self.camera_config
        if self.ip_override:
            desired_ip = self.ip_override
        else:
            cam_number = _cam_number(cfg.id)
            if cam_number is None:
                self.logger.warning(
                    "cam=%s id has no trailing number; skip IP assign", cfg.id)
                return
            desired_ip = f"{_GIGE_SUBNET_PREFIX}.{_GIGE_IP_OFFSET + cam_number}"

        try:
            gige_tl = tl_factory.CreateTl("BaslerGigE")
        except Exception as error:
            self.logger.warning(
                "cam=%s GigE TL unavailable err=%s", cfg.id, error)
            return

        mac, current_ip = self._find_on_subnet(gige_tl, cfg.serial)
        if mac is None:
            self.logger.warning(
                "cam=%s serial=%s not found on GigE subnet", cfg.id, cfg.serial)
            return
        if current_ip == desired_ip:
            self.logger.debug(
                "cam=%s already at %s; skip ForceIp", cfg.id, desired_ip)
            return

        try:
            gige_tl.ForceIp(mac, desired_ip, _GIGE_NETMASK, _GIGE_GATEWAY)
        except Exception as error:
            self.logger.warning(
                "ForceIp failed cam=%s serial=%s ip=%s err=%s",
                cfg.id, cfg.serial, desired_ip, error)
            return

        self.logger.info(
            "ForceIp cam=%s serial=%s mac=%s %s->%s",
            cfg.id, cfg.serial, mac, current_ip, desired_ip)

        if not self._wait_for_ip(gige_tl, cfg.serial, desired_ip):
            self.logger.warning(
                "cam=%s not at %s within %.0fs after ForceIp "
                "(continuing; open retries)",
                cfg.id, desired_ip, _FORCEIP_SETTLE_TIMEOUT_S)

    def _find_on_subnet(self, gige_tl, serial):
        for device in gige_tl.EnumerateDevices():
            if device.GetSerialNumber() == serial:
                return device.GetMacAddress(), device.GetIpAddress()
        return None, None

    def _wait_for_ip(self, gige_tl, serial, desired_ip) -> bool:
        deadline = time.monotonic() + _FORCEIP_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            _, current_ip = self._find_on_subnet(gige_tl, serial)
            if current_ip == desired_ip:
                return True
            time.sleep(_FORCEIP_POLL_INTERVAL_S)
        return False

    def _apply_defaults(self, pylon_camera) -> None:
        acq = self.config.acq
        _try_set(pylon_camera, "PixelFormat",
                 "Mono8" if acq.channels == 1 else "RGB8")
        _try_set(pylon_camera, "Width",  acq.width)
        _try_set(pylon_camera, "Height", acq.height)
        try:
            pylon_camera.AcquisitionFrameRateEnable.SetValue(True)
        except Exception:
            pass
        if not _try_set(pylon_camera, "AcquisitionFrameRateAbs", self.config.encode.fps):
            _try_set(pylon_camera, "AcquisitionFrameRate", self.config.encode.fps)

        _try_set(pylon_camera, "ExposureAuto", "Off")
        _try_set(pylon_camera, "GainAuto", "Off")
        if not _try_set(pylon_camera, "ExposureTimeAbs", _DEFAULT_EXPOSURE_US):
            _try_set(pylon_camera, "ExposureTime", _DEFAULT_EXPOSURE_US)
        if not _try_set(pylon_camera, "GainRaw", int(_DEFAULT_GAIN)):
            _try_set(pylon_camera, "Gain", _DEFAULT_GAIN)

    def _apply_gige_tuning(self, pylon_camera) -> None:
        packet_size = _PACKET_SIZE_JUMBO if self.config.acq.jumbo_frames else _PACKET_SIZE_STANDARD
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
