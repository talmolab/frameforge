"""Verb registry: every bounded request/response a node answers.

``@verb`` on a handler method registers it. The CLI, the management server and the
client all build from ``VERBS``; nothing is declared twice. The HTTP conventions
the server and client share live here too. Interactive flows (setup) are not verbs.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Arg:
    """flag: a boolean switch. body: a JSON structure carried in the request body."""
    name: str
    help: str
    type: type = str
    default: object = None
    flag: bool = False
    body: bool = False


@dataclass(frozen=True, slots=True)
class Verb:
    """returns names an entrypoint.types class, "list[Name]", or "lines" for a stream.
    mutates selects POST with a token over GET. cli hides body-only verbs from ``ff``."""
    name: str
    help: str
    returns: str
    args: tuple[Arg, ...]
    mutates: bool
    streams: bool
    cli: bool


VERBS: dict[str, Verb] = {}


def verb(help: str, *args: Arg, returns: str = "ActionResult", mutates: bool = True,
         streams: bool = False, cli: bool = True) -> Callable:
    def register(fn):
        VERBS[fn.__name__] = Verb(fn.__name__, help, returns, args, mutates, streams, cli)
        return fn
    return register


TOKEN_HEADER = "X-FF-Token"
SETUP_PATH = "/setup/{mode}"
SETUP_QUESTIONS_PATH = "/setup/{mode}/questions"


def route(spec: Verb) -> tuple[str, str]:
    return ("POST" if spec.mutates else "GET"), f"/api/{spec.name}"
