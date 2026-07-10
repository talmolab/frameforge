"""Basler camera lifecycle: discover, open, configure, GigE tune."""

import logging
import os

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


class Camera:
    def __init__(self, camera_config: CameraCfg, full_config: Config) -> None:
        self.camera_config = camera_config
        self.config = full_config
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
        slot = next(
            (i + 1 for i, cam in enumerate(self.config.cameras)
             if cam.id == cfg.id),
            0,
        )
        if slot == 0:
            self.logger.warning("cam=%s not in config.cameras; skip IP assign", cfg.id)
            return
        desired_ip = f"{_GIGE_SUBNET_PREFIX}.{_GIGE_IP_OFFSET + slot}"
        try:
            gige_tl = tl_factory.CreateTl("BaslerGigE")
            mac = next(
                (dev.GetMacAddress() for dev in gige_tl.EnumerateDevices()
                 if dev.GetSerialNumber() == cfg.serial),
                None,
            )
            if mac is None:
                self.logger.warning(
                    "cam=%s serial=%s not found on GigE subnet", cfg.id, cfg.serial)
                return
            gige_tl.ForceIp(mac, desired_ip, _GIGE_NETMASK, _GIGE_GATEWAY)
            self.logger.info(
                "ForceIp cam=%s serial=%s mac=%s ip=%s",
                cfg.id, cfg.serial, mac, desired_ip)
        except Exception as error:
            self.logger.warning(
                "ForceIp failed cam=%s serial=%s ip=%s err=%s",
                cfg.id, cfg.serial, desired_ip, error)

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

    def _apply_gige_tuning(self, pylon_camera) -> None:
        packet_size = _PACKET_SIZE_JUMBO if self.config.acq.jumbo_frames else _PACKET_SIZE_STANDARD
        _try_set(pylon_camera, "GevSCPSPacketSize", packet_size)
        _try_set(pylon_camera, "GevSCPD",           _INTER_PACKET_DELAY_NS)
        try:
            pylon_camera.MaxNumBuffer.SetValue(_MAX_NUM_BUFFER)
        except Exception:
            pass


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
