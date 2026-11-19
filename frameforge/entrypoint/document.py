"""The config document: tenant.yaml + cameras.yaml as one object, versioned on write.

A write validates the posted document through ``Config.validate`` before touching
disk, snapshots the previous files, writes, bumps the version, then re-reads and
validates again. Hand edits are allowed and show as ``hand_edited`` until the next
write. The running process records what it loaded so status can say whether a
restart is required.
"""

import dataclasses
import hashlib
import json
import logging
import os
import shutil

import yaml

from .. import config as runtime
from ..core.paths import CAMERAS_FILE, STATE_DIR, TENANT_FILE
from .setup import write_yaml
from .types import ActionResult, ConfigDocument, ConfigField, ConfigState

VERSION_FILE = os.path.join(STATE_DIR, "config-version.json")
APPLIED_FILE = os.path.join(STATE_DIR, "config-applied.json")
HISTORY_DIR = os.path.join(STATE_DIR, "config-history")

_SECTIONS = (
    ("encode", runtime.EncodeCfg, None),
    ("acq", runtime.AcqCfg, None),
    ("transfer", runtime.TransferCfg, ("low_disk_threshold_mb", "analytics")),
    ("transfer.storage", runtime.StorageCfg, None),
    ("session", runtime.Config, ("session_name", "session_postfix")),
)

logger = logging.getLogger("frameforge.entrypoint.document")


def read() -> ConfigDocument:
    tenant, cameras, digest = _files()
    record = _record()
    return ConfigDocument(
        version=record["version"], hash=digest,
        hand_edited=record["hash"] is not None and record["hash"] != digest,
        tenant=tenant, cameras=cameras)


def state() -> ConfigState:
    document = read()
    applied = _read_json(APPLIED_FILE)
    return ConfigState(
        version=document.version, hash=document.hash, hand_edited=document.hand_edited,
        version_applied=None if applied is None else applied["version"],
        restart_required=None if applied is None else applied["hash"] != document.hash)


def write(tenant: dict, cameras: list[dict], if_version: int | None = None) -> ActionResult:
    record = _record()
    if if_version is not None and if_version != record["version"]:
        return ActionResult("config_put", False,
                            f"version mismatch: on disk is {record['version']}, sent {if_version}")
    problem = _validate(tenant, cameras)
    if problem:
        return ActionResult("config_put", False, problem)

    _snapshot(record["version"])
    write_yaml(TENANT_FILE, tenant)
    write_yaml(CAMERAS_FILE, {"cameras": cameras})

    tenant_back, cameras_back, digest = _files()
    problem = _validate(tenant_back, cameras_back)
    if problem:
        return ActionResult("config_put", False, f"written but failed re-validation: {problem}")
    version = record["version"] + 1
    _write_json(VERSION_FILE, {"version": version, "hash": digest})
    return ActionResult("config_put", True, f"config version {version}")


def schema() -> list[ConfigField]:
    fields = []
    for section, cls, only in _SECTIONS:
        for spec in dataclasses.fields(cls):
            if spec.name == "storage" or (only is not None and spec.name not in only):
                continue
            default = None if spec.default is dataclasses.MISSING else spec.default
            fields.append(ConfigField(section, spec.name, _type_name(spec.type), default,
                                      spec.metadata.get("doc", "")))
    return fields


def record_applied() -> None:
    try:
        _, _, digest = _files()
        _write_json(APPLIED_FILE, {"version": _record()["version"], "hash": digest})
    except OSError as error:
        logger.warning("could not record applied config: %s", error)


def _files() -> tuple[dict, list, str]:
    digest = hashlib.sha256()
    loaded = []
    for path in (TENANT_FILE, CAMERAS_FILE):
        try:
            with open(path, "rb") as raw:
                data = raw.read()
        except OSError:
            data = b""
        digest.update(data)
        loaded.append(yaml.safe_load(data) or {})
    return loaded[0], loaded[1].get("cameras", []), digest.hexdigest()[:16]


def _validate(tenant: dict, cameras: list[dict]) -> str | None:
    try:
        config = runtime.build_config(tenant, cameras)
        config.validate()
    except (ValueError, TypeError) as error:
        return str(error)
    return _check_types(config)


_ACCEPTED = {int: (int,), float: (int, float), bool: (bool,), str: (str,), dict: (dict,)}


def _check_types(config: runtime.Config) -> str | None:
    for section, cls, only in _SECTIONS:
        holder = _section_object(config, section)
        for spec in dataclasses.fields(cls):
            if spec.name == "storage" or (only is not None and spec.name not in only):
                continue
            accepted = _ACCEPTED.get(spec.type)
            value = getattr(holder, spec.name)
            if accepted and (not isinstance(value, accepted) or isinstance(value, bool) and spec.type is not bool):
                return f"config: {section}.{spec.name} must be {spec.type.__name__}, got {value!r}"
    return None


def _section_object(config: runtime.Config, section: str):
    holder = config
    if section != "session":
        for part in section.split("."):
            holder = getattr(holder, part)
    return holder


def _snapshot(version: int) -> None:
    target = os.path.join(HISTORY_DIR, f"v{version}")
    os.makedirs(target, exist_ok=True)
    for path in (TENANT_FILE, CAMERAS_FILE):
        if os.path.exists(path):
            shutil.copy2(path, target)


def _record() -> dict:
    return _read_json(VERSION_FILE) or {"version": 0, "hash": None}


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as raw:
            return json.load(raw)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as raw:
        json.dump(data, raw, indent=1)
        raw.write("\n")
    os.replace(tmp_path, path)


def _type_name(annotation) -> str:
    return getattr(annotation, "__name__", str(annotation))
