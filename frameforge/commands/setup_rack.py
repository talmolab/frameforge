"""Rack mode: cameras are cabled. cameras.yaml maps cam_XX ids to serials; the id is
explicit (it fixes the camera's IP) and the list is free to renumber. The operator
edits the mapping, sees the diff, and frameforge (re)starts at the chunk boundary.
"""

import re

from ..core.paths import CAMERAS_FILE
from ..sources import validate_source
from .flows import Prompter, Question, collect
from .setup import read_cameras, write_yaml
from .types import ActionResult, DiscoveredCamera

_ID_PATTERN = re.compile(r"^cam_\d{2,}$")


def questions(found: list[DiscoveredCamera], current: dict[str, str]) -> list[Question]:
    """One question: the id=serial mapping. Default keeps the file and adds new serials."""
    on_switch = [cam.serial for cam in found]
    mapping = {cam_id: serial for cam_id, serial in current.items() if serial in on_switch}
    for serial in on_switch:
        if serial not in mapping.values():
            mapping[_next_free_id(mapping)] = serial
    listing = "   ".join(f"{cam.serial} ({cam.model}, {cam.ip or 'no ip'})" for cam in found)
    return [Question(
        "cameras", "Cameras as cam_XX=serial, space separated",
        f"on the switch: {listing}. Drop a pair to remove it; change the id to renumber.",
        default=_format(mapping),
        validate=lambda value: _check(value, set(on_switch) | set(current.values())))]


def run(node, prompter: Prompter) -> ActionResult:
    found = node.cams()
    if not found:
        return ActionResult("setup", False, "no cameras found on the switch")
    current = {cam["id"]: str(cam.get("serial", "")) for cam in read_cameras()}
    missing = [s for s in current.values() if s not in {cam.serial for cam in found}]
    if missing:
        prompter.say("in cameras.yaml but not on the switch: " + ", ".join(missing))

    mapping = _parse(collect(questions(found, current), prompter)["cameras"])
    if not mapping:
        return ActionResult("setup", False, "no cameras given; cameras.yaml unchanged")
    for serial in mapping.values():
        validate_source("pylon", {"serial": serial})

    diff = _diff(current, mapping)
    prompter.say(diff or "no change")
    if diff and not prompter.confirm("Write cameras.yaml?"):
        return ActionResult("setup", False, "cancelled; cameras.yaml unchanged")
    if diff:
        write_yaml(CAMERAS_FILE, {"cameras": [
            {"id": cam_id, "serial": serial} for cam_id, serial in sorted(mapping.items())]})

    running = node.system.is_active("frameforge") == "active"
    outcome = node.restart() if running else node.start()
    return ActionResult("setup", outcome.ok, "\n".join([
        f"{len(mapping)} camera(s) in {CAMERAS_FILE}",
        f"{outcome.action}: {'ok' if outcome.ok else outcome.detail}",
    ]))


def _format(mapping: dict[str, str]) -> str:
    return " ".join(f"{cam_id}={serial}" for cam_id, serial in sorted(mapping.items()))


def _parse(value: str) -> dict[str, str]:
    pairs = [token.split("=", 1) for token in value.replace(",", " ").split()]
    return {cam_id: serial for cam_id, serial in pairs}


def _check(value: str, known: set[str]) -> str | None:
    tokens = value.replace(",", " ").split()
    if any("=" not in token for token in tokens):
        return "every entry must look like cam_01=24750381"
    pairs = [token.split("=", 1) for token in tokens]
    ids = [cam_id for cam_id, _ in pairs]
    serials = [serial for _, serial in pairs]
    bad = [cam_id for cam_id in ids if not _ID_PATTERN.match(cam_id)]
    if bad:
        return "ids must be cam_XX: " + ", ".join(bad)
    if len(ids) != len(set(ids)):
        return "an id is used twice"
    if len(serials) != len(set(serials)):
        return "a serial is listed twice"
    unknown = [s for s in serials if s not in known]
    if unknown:
        return "not on the switch: " + ", ".join(unknown)
    return None


def _next_free_id(mapping: dict[str, str]) -> str:
    n = 1
    while f"cam_{n:02d}" in mapping:
        n += 1
    return f"cam_{n:02d}"


def _diff(old: dict[str, str], new: dict[str, str]) -> str:
    lines = []
    for cam_id in sorted(set(old) | set(new)):
        if old.get(cam_id) == new.get(cam_id):
            continue
        if cam_id in old:
            lines.append(f"- {cam_id}: {old[cam_id]}")
        if cam_id in new:
            lines.append(f"+ {cam_id}: {new[cam_id]}")
    return "\n".join(lines)
