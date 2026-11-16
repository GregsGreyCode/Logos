#!/usr/bin/env python3
"""``logos``: host-side control for the Logos gateway.

    logos gateway [run]      Run the gateway in the foreground
    logos gateway start      Start the gateway in the background
    logos gateway stop       Stop the running gateway
    logos gateway restart    Stop, then start in the background
    logos gateway status     Show whether the gateway is running
    logos doctor             Check the host (python, docker, openshell, home, auth DB)
    logos --version          Show version

Agents run the upstream ``hermes`` CLI inside their OpenShell sandboxes and are
managed with ``scripts/logos-agent``; this CLI only controls the gateway.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``gateway`` importable when run as ``python logos_cli/main.py``.
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logos_cli import __version__  # noqa: E402


def cmd_gateway(args) -> None:
    from logos_cli.gateway import gateway_command
    gateway_command(args)


def cmd_doctor(args) -> None:
    from logos_cli.doctor import run_doctor
    sys.exit(run_doctor(args))


def cmd_version(args) -> None:
    print(f"Logos v{__version__}")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Python: {sys.version.split()[0]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logos",
        description="Logos gateway control",
    )
    parser.add_argument("--version", "-V", action="store_true", help="Show version and exit")
    subparsers = parser.add_subparsers(dest="command")

    gateway_parser = subparsers.add_parser("gateway", help="Run and control the gateway")
    gateway_sub = gateway_parser.add_subparsers(dest="gateway_command")
    gateway_run = gateway_sub.add_parser("run", help="Run the gateway in the foreground (default)")
    gateway_run.add_argument(
        "--replace", action="store_true",
        help="Stop an already-running gateway for this LOGOS_HOME first",
    )
    gateway_sub.add_parser("start", help="Start the gateway in the background")
    gateway_sub.add_parser("stop", help="Stop the running gateway")
    gateway_sub.add_parser("restart", help="Restart the gateway in the background")
    gateway_sub.add_parser("status", help="Show whether the gateway is running")
    gateway_sub.add_parser("setup-link", help="Show the one-time link for creating the first account")
    gateway_parser.set_defaults(func=cmd_gateway)

    doctor_parser = subparsers.add_parser("doctor", help="Check the host setup")
    doctor_parser.set_defaults(func=cmd_doctor)

    version_parser = subparsers.add_parser("version", help="Show version")
    version_parser.set_defaults(func=cmd_version)

    return parser


def main(argv=None) -> None:
    """Entry point for the ``logos`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        cmd_version(args)
        return
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
