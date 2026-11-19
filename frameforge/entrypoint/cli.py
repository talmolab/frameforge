"""``ff``: argparse surface generated from the verb registry, plus the interactive
``setup`` flow mounted by hand."""

import argparse
import dataclasses
import json
import sys

from .node import Node
from .verbs import VERBS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ff", description="frameforge node control")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    for spec in VERBS.values():
        if not spec.cli:
            continue
        command = sub.add_parser(spec.name, help=spec.help)
        for arg in spec.args:
            flag = ("-" if len(arg.name) == 1 else "--") + arg.name
            if arg.flag:
                command.add_argument(flag, dest=arg.name, action="store_true", help=arg.help)
            else:
                command.add_argument(flag, dest=arg.name, type=arg.type,
                                     default=arg.default, help=arg.help)

    setup = sub.add_parser("setup", help="desk: configure a fresh box; rack: assign cameras")
    setup.add_argument("--mode", choices=("desk", "rack"), default="desk")
    setup.add_argument("--yes", action="store_true", help="skip confirmations")

    serve = sub.add_parser("serve", help="management server for the hub (ff-serve.service)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--bind", default="0.0.0.0")
    return parser


def _plain(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _render(value, indent: int = 0) -> None:
    pad = "   " * indent
    value = _plain(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{pad}{key}:")
                _render(item, indent + 1)
            else:
                print(f"{pad}{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and not any(isinstance(v, (dict, list)) for v in item.values()):
                print(pad + "  ".join(f"{k}={v}" for k, v in item.items()))
            else:
                _render(item, indent)
    else:
        print(f"{pad}{value}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "serve":
        from ..core.logging_setup import setup_logging
        from .server import serve

        setup_logging()
        serve(port=args.port, bind=args.bind)
        return 0

    node = Node()
    if args.command == "setup":
        outcome = node.setup(mode=args.mode, yes=args.yes)
    else:
        spec = VERBS[args.command]
        outcome = getattr(node, spec.name)(**{arg.name: getattr(args, arg.name) for arg in spec.args})
        if spec.streams:
            try:
                for line in outcome:
                    print(line, flush=True)
            except KeyboardInterrupt:
                pass
            return 0

    if args.json:
        print(json.dumps(_plain(outcome), indent=1))
    else:
        _render(outcome)
    return 0 if getattr(outcome, "ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
