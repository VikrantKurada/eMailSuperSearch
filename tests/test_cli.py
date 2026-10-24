import json

import pytest

from emailsupersearch import cli
from emailsupersearch.cli import main


def test_cli_doctor_exits_zero(capsys): assert main(["doctor"]) == 0 and "python_build" in capsys.readouterr().out
def test_cli_doctor_json(capsys): main(["doctor", "--json"]); assert json.loads(capsys.readouterr().out)["checks"]
def test_cli_version(capsys): assert main(["--version"]) == 0 and "0.0.1" in capsys.readouterr().out


@pytest.fixture
def commands(monkeypatch):
    monkeypatch.setattr(cli, "_COMMANDS", dict(cli._COMMANDS))
    return cli._COMMANDS


def test_registered_command_receives_its_arguments(commands, capsys):
    def configure(p):
        p.add_argument("--n", type=int, default=1)

    cli.register("ping", lambda args: args.n + 1, configure)
    assert main(["ping", "--n", "4"]) == 5


def test_no_subcommand_runs_serve_when_registered(commands):
    cli.register("serve", lambda args: 7, lambda p: None)
    assert main([]) == 7


def test_no_subcommand_prints_help_when_serve_is_absent(commands, capsys):
    commands.pop("serve", None)
    assert main([]) == 0 and "doctor" in capsys.readouterr().out


def test_bad_arguments_return_the_parser_exit_code(capsys):
    assert main(["doctor", "--nope"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err
