"""``ff``: argparse surface generated from the verb registry."""

import argparse
import dataclasses
import json
import sys

from .node import Node
from .verbs import VERBS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ff", description="frameforge node control")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="verb", required=True)
    for spec in VERBS.values():
        command = sub.add_parser(spec.name, help=spec.help)
        for arg in spec.args:
            flag = ("-" if len(arg.name) == 1 else "--") + arg.name
            if arg.flag:
                command.add_argument(flag, dest=arg.name, action="store_true", help=arg.help)
            else:
                command.add_argument(flag, dest=arg.name, type=arg.type,
                                     default=arg.default, help=arg.help)
    return parser


def _render(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_plain(obj), indent=1))
        return
    if isinstance(obj, list):
        for item in obj:
            print("  ".join(f"{k}={v}" for k, v in _plain(item).items()))
        return
    for key, value in _plain(obj).items():
        if isinstance(value, list):
            print(f"{key}:")
            for item in value:
                print("   " + "  ".join(f"{k}={v}" for k, v in item.items()))
        elif isinstance(value, dict):
            print(f"{key}:")
            for k, v in value.items():
                print(f"   {k}: {v}")
        else:
            print(f"{key}: {value}")


def _plain(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_plain(item) for item in obj]
    return obj


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = VERBS[args.verb]
    kwargs = {arg.name: getattr(args, arg.name) for arg in spec.args}
    outcome = getattr(Node(), spec.method)(**kwargs)

    if spec.streams:
        try:
            for line in outcome:
                print(line, flush=True)
        except KeyboardInterrupt:
            pass
        return 0

    _render(outcome, args.json)
    return 0 if getattr(outcome, "ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
