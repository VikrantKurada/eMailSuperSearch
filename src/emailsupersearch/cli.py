"""Command-line entry point: ``python -m emailsupersearch <command>``.

Commands register themselves with `register`; with no command, `serve` runs when it is registered.
"""

import argparse
import dataclasses
import inspect
import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from emailsupersearch import __version__
from emailsupersearch.diagnostics.doctor import render_report, report_json, run_doctor
from emailsupersearch.paths import (
    data_root,
    ensure_layout,
    project_dir,
    validate_data_root,
)

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


def _bench(args: argparse.Namespace) -> int:
    """Time parsing, embedding and nearest-neighbour search on a synthetic or given mail folder."""
    # Imported here, not at the top: bench loads numpy and onnxruntime, and `doctor` must still run
    # on a machine where one of those native modules is blocked.
    from emailsupersearch.diagnostics.bench import run_bench

    report = dataclasses.asdict(run_bench(args.corpus, vectors=args.vectors))
    if args.json:
        _print(json.dumps(report, indent=2))
        return 0
    for name, value in report.items():
        if name == "notes":
            value = " | ".join(value) or "none"
        _print(f"{name}: {'n/a' if value is None else value}")
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is not a positive number")
    return value


def _configure_bench(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", type=Path, help="folder of .eml/.msg files to time instead of the synthetic corpus")
    parser.add_argument(
        "--vectors", type=_positive_int, default=300_000, help="rows in the nearest-neighbour test (default 300000)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


register("bench", _bench, _configure_bench)


def _embedding_dir() -> Path | None:
    """The installed embedding model's folder, or None without the models package."""
    try:
        from emailsupersearch_models_en import embedding_dir
    except ImportError:
        return None
    return embedding_dir()


def _sync(args: argparse.Namespace) -> int:
    """Reconcile a project's mail folders and index what changed; with --once, one pass and exit."""
    root = data_root()
    reason = validate_data_root(root)
    if reason is not None:
        print(reason, file=sys.stderr)
        return 2
    if not (project_dir(root, args.project) / "project.sqlite").is_file():  # sync never creates a project
        print(f"unknown project: {args.project}", file=sys.stderr)
        return 2
    ensure_layout(root)
    # Imported here, not at the top, for the reason given in `_bench`.
    from emailsupersearch.index.embedder import load_default_embedder, tokenizer_path
    from emailsupersearch.ingest.chunker import Chunker
    from emailsupersearch.jobs.supervisor import build_project_supervisor
    from emailsupersearch.store import ProjectStore

    store = ProjectStore.open(project_dir(root, args.project))
    embedder = load_default_embedder()
    model_dir = _embedding_dir() if embedder is not None else None
    chunker = Chunker(tokenizer_path(model_dir) if model_dir is not None else None)
    sup = build_project_supervisor(args.project, store, embedder, chunker, root / "cache" / "parsed", on_event=None)
    if args.once:
        try:
            sup.prepare()  # a pending re-index and the failed-file retry, as start() would do
            r = sup.run_once()
        finally:
            sup.stop()
        _print(
            f"scan {r.scan_id}: parsed {len(r.to_parse)}, renamed {len(r.renamed)},"
            f" tombstoned {len(r.tombstoned)}, purged {len(r.purged)}, errors {len(r.errors)}"
        )
        return 0
    sup.start()
    forever = threading.Event()
    try:
        while not forever.wait(1.0):  # a timed wait, so Ctrl+C is noticed on Windows too
            pass
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop()
    return 0


def _configure_sync(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="the project's id")
    parser.add_argument("--once", action="store_true", help="run one reconcile pass and exit")


register("sync", _sync, _configure_sync)


def _port(text: str) -> int:
    value = _positive_int(text)
    if value > 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a port number")
    return value


def _serve(args: argparse.Namespace) -> int:
    """Start the host and open the browser on it (or on the host already running); runs until stopped."""
    # Imported here, not at the top: the launcher's start-up loads native modules, see `_bench`.
    from emailsupersearch.host.launcher import resolve_root, serve

    return serve(resolve_root(), args.port, args.headless, open_browser=not args.no_browser)


def _configure_serve(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=_port, help="port to listen on (default: the saved setting, else 8765)")
    parser.add_argument(
        "--headless", action="store_true", help="print nothing; console output goes to logs/host-stderr.log"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")


register("serve", _serve, _configure_serve)


def _stop(args: argparse.Namespace) -> int:
    """Stop the running host."""
    from emailsupersearch.host.launcher import resolve_root, stop_any

    # The default root too: after a data_root change the host still runs from the root it started on.
    return stop_any([resolve_root(), data_root()])


register("stop", _stop, lambda parser: None)


def _purge(args: argparse.Namespace) -> int:
    """Stop the host, then remove the data root: every project, index, cache and log."""
    if not args.yes:
        print("refusing to purge without --yes", file=sys.stderr)
        return 2
    from emailsupersearch.host.launcher import remove_root, resolve_root, stop

    root = resolve_root()
    if stop(root) != 0:
        print("the host did not stop; nothing was removed", file=sys.stderr)
        return 1
    problem = remove_root(root)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    return 0


def _configure_purge(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="confirm: every project, index, cache and log is deleted")


register("purge", _purge, _configure_purge)
