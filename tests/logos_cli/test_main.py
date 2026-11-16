"""Tests for the trimmed ``logos`` entry point (S1.8)."""

import re
from pathlib import Path

import pytest

from logos_cli import __version__, main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_version_flag_prints_version(capsys):
    cli_main.main(["--version"])
    out = capsys.readouterr().out
    assert f"Logos v{__version__}" in out
    assert "Python:" in out


def test_version_subcommand(capsys):
    cli_main.main(["version"])
    assert f"Logos v{__version__}" in capsys.readouterr().out


def test_no_args_prints_help(capsys):
    cli_main.main([])
    out = capsys.readouterr().out
    assert "gateway" in out and "doctor" in out


def test_parser_exposes_only_gateway_doctor_version():
    parser = cli_main.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == {"gateway", "doctor", "version"}
    gw = next(a for a in sub.choices["gateway"]._actions if a.dest == "gateway_command")
    assert set(gw.choices) == {"run", "start", "stop", "restart", "status", "setup-link"}


@pytest.mark.parametrize("removed", ["setup", "update", "config", "claw", "pairing", "uninstall", "login"])
def test_removed_commands_are_rejected(removed):
    with pytest.raises(SystemExit) as exc:
        cli_main.main([removed])
    assert exc.value.code == 2


def test_gateway_dispatches_to_gateway_command(monkeypatch):
    seen = []
    import logos_cli.gateway as gw
    monkeypatch.setattr(gw, "gateway_command", lambda args: seen.append(args.gateway_command))
    cli_main.main(["gateway", "status"])
    cli_main.main(["gateway"])
    assert seen == ["status", None]


def test_doctor_exit_code_propagates(monkeypatch):
    import logos_cli.doctor as doctor
    monkeypatch.setattr(doctor, "run_doctor", lambda args=None: 1)
    with pytest.raises(SystemExit) as exc:
        cli_main.main(["doctor"])
    assert exc.value.code == 1


def test_logos_cli_package_is_only_the_trimmed_modules():
    modules = {p.name for p in (REPO_ROOT / "logos_cli").glob("*.py")}
    assert modules == {"__init__.py", "main.py", "gateway.py", "doctor.py"}


def test_gateway_does_not_import_logos_cli():
    """gateway/ must not depend on logos_cli (the CLI imports the gateway)."""
    pat = re.compile(r"^\s*(from|import)\s+logos_cli\b", re.MULTILINE)
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "gateway").rglob("*.py")
        if pat.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
