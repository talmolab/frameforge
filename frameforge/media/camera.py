"""Basler camera lifecycle: discover, open, configure (.pfs or YAML), GigE tune."""

import logging
import os

from pypylon import pylon

from ..core.config import AcqCfg, CameraCfg, Config


class Camera:
    def __init__(self, camera_config: CameraCfg, full_config: Config) -> None:
        self.camera_config = camera_config
        self.config = full_config
        self.logger = logging.getLogger("frameforge.camera")

        self.pylon_camera = None
        self.retrieve_timeout_ms = 3000

    def open(self) -> None:
        tl_factory = pylon.TlFactory.GetInstance()
        if self.camera_config.serial:
            device_info = pylon.DeviceInfo()
            device_info.SetSerialNumber(self.camera_config.serial)
            pylon_camera = pylon.InstantCamera(
                tl_factory.CreateDevice(device_info))
        else:
            pylon_camera = pylon.InstantCamera(tl_factory.CreateFirstDevice())

        pylon_camera.Open()
        self.pylon_camera = pylon_camera

        if self.camera_config.pfs and os.path.isfile(self.camera_config.pfs):
            pylon.FeaturePersistence.Load(
                self.camera_config.pfs, pylon_camera.GetNodeMap(), True)
            self.logger.info("cam=%s applied .pfs %s",
                             self.camera_config.id, self.camera_config.pfs)
        else:
            self._apply_yaml_defaults(pylon_camera)

        self._apply_gige_tuning(pylon_camera, self.config.acq)
        self.retrieve_timeout_ms = (
            self.config.acq.retrieve_timeout_ms
            or _heartbeat_ms(pylon_camera)
            or 3000
        )

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

    def _apply_yaml_defaults(self, pylon_camera) -> None:
        _try_set(pylon_camera, "PixelFormat",
                 "Mono8" if self.config.channels == 1 else "RGB8")
        _try_set(pylon_camera, "Width",  self.config.width)
        _try_set(pylon_camera, "Height", self.config.height)
        try:
            pylon_camera.AcquisitionFrameRateEnable.SetValue(True)
        except Exception:
            pass
        if not _try_set(pylon_camera, "AcquisitionFrameRateAbs", self.config.encode.fps):
            _try_set(pylon_camera, "AcquisitionFrameRate", self.config.encode.fps)

    # YAML/.pfs win for the initial layout; GigE knobs are applied last
    # so an operator's transient tuning doesn't get clobbered.
    def _apply_gige_tuning(self, pylon_camera, acq_config: AcqCfg) -> None:
        _try_set(pylon_camera, "GevSCPSPacketSize", acq_config.packet_size)
        _try_set(pylon_camera, "GevSCPD",           acq_config.inter_packet_delay_ns)
        try:
            pylon_camera.MaxNumBuffer.SetValue(acq_config.max_num_buffer)
        except Exception:
            pass


# Not every Basler model exposes every node; best-effort tuning instead
# of aborting init on a missing GenICam feature.
def _try_set(pylon_camera, node_name, value) -> bool:
    try:
        pylon_camera.GetNodeMap().GetNode(node_name).SetValue(value)
        return True
    except Exception:
        return False


# Fallback for retrieve timeout when YAML didn't pin one: matches the
# camera's own heartbeat so a single missed-heartbeat = single grab fail
# instead of an unbounded block.
def _heartbeat_ms(pylon_camera) -> int:
    try:
        return int(pylon_camera.GevHeartbeatTimeout.GetValue())
    except Exception:
        return 0
