"""Verb registry. ``@verb`` annotates a Node method with its help text and arguments;
the CLI and the management server both build their surfaces from ``VERBS``.
"""

import dataclasses
from collections.abc import Callable


@dataclasses.dataclass(frozen=True)
class Arg:
    name: str
    help: str
    type: type = str
    default: object = None
    flag: bool = False      # boolean switch rather than a value


@dataclasses.dataclass(frozen=True)
class Verb:
    name: str
    help: str
    args: tuple[Arg, ...]
    mutates: bool           # False = read-only (GET), True = changes state (POST)
    streams: bool           # yields lines rather than returning one object
    method: str


VERBS: dict[str, Verb] = {}


def verb(help: str, *args: Arg, mutates: bool = True, streams: bool = False) -> Callable:
    def register(fn):
        VERBS[fn.__name__] = Verb(fn.__name__, help, args, mutates, streams, fn.__name__)
        return fn
    return register
