"""Node: the verbs from handlers plus the interactive setup flow.

The CLI, the management server and the client all drive this class through the
verb registry. It never prints. Setup is a flow, not a verb, and each surface
mounts it separately.
"""

from . import flows
from .handlers import Configure, Control, Monitor
from .setup import read_cameras
from .system import SystemAdapter
from .types import ActionResult


class Node(Monitor, Control, Configure):
    def __init__(self, system: SystemAdapter | None = None) -> None:
        self.system = system or SystemAdapter()

    def setup(self, mode: str = "desk", yes: bool = False,
              answers: dict | None = None) -> ActionResult:
        """desk: configure a fresh box. rack: map cameras on the switch to ids."""
        from . import setup_desk, setup_rack

        prompter = flows.Answers(answers, assume_yes=yes) if answers is not None \
            else flows.Prompter()
        flow = {"desk": setup_desk, "rack": setup_rack}.get(mode)
        if flow is None:
            return ActionResult("setup", False, f"unknown mode {mode!r}; use desk or rack")
        try:
            return flow.run(self, prompter)
        except ValueError as error:
            return ActionResult("setup", False, str(error))

    def setup_questions(self, mode: str) -> list[dict]:
        from . import setup_desk, setup_rack

        if mode == "desk":
            return flows.describe(setup_desk.questions() + setup_desk.storage_questions())
        if mode == "rack":
            current = {cam["id"]: str(cam.get("serial", "")) for cam in read_cameras()}
            return flows.describe(setup_rack.questions(self.cams(), current))
        raise ValueError(f"unknown mode {mode!r}; use desk or rack")
