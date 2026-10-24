"""Command-line entry point: ``python -m emailsupersearch <command>``.

Commands register themselves with `register`; with no command, `serve` runs when it is registered.
"""

import argparse
import inspect
import sys
from collections.abc import Callable

from emailsupersearch import __version__
from emailsupersearch.diagnostics.doctor import render_report, report_json, run_doctor

Handler = Callable[[argparse.Namespace], int]
Configure = Callable[[argparse.ArgumentParser], None]

_COMMANDS: dict[str, tuple[Handler, Configure]] = {}


def register(name: str, fn: Handler, configure: Configure) -> None:
    """Add a sub-command: `configure` declares its arguments, `fn` runs it and returns the exit code."""
    _COMMANDS[name] = (fn, configure)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emailsupersearch", description="Offline e-mail search for Windows.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    for name, (fn, configure) in _COMMANDS.items():
        summary = (inspect.getdoc(fn) or "").split("\n")[0] or None
        configure(commands.add_parser(name, help=summary, description=summary))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None and "serve" in _COMMANDS:
            args = parser.parse_args(["serve"])
    except SystemExit as e:
        return int(e.code or 0)
    if args.command is None:
        parser.print_help()
        return 0
    return int(_COMMANDS[args.command][0](args))


def _print(text: str) -> None:
    """Print without ever failing on a character the console cannot encode."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding))


def _doctor(args: argparse.Namespace) -> int:
    """Check this machine and print a redacted environment report."""
    report = run_doctor()
    _print(report_json(report) if args.json else render_report(report))
    return 0


def _configure_doctor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


register("doctor", _doctor, _configure_doctor)
