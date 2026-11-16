"""``logos doctor``: host checks for running the Logos gateway.

Checks: Python version, Docker daemon reachable, OpenShell CLI present (and
its version), LOGOS_HOME writable, auth DB opens.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Callable, List, Optional, Tuple

from gateway.local_config import get_logos_home

MIN_PYTHON = (3, 11)
# OpenShell release the v2 infra targets (see infra/openshell/README.md).
EXPECTED_OPENSHELL = "0.1.8"

OK, WARN, FAIL = "ok", "warn", "fail"
_MARKS = {OK: "✓", WARN: "⚠", FAIL: "✗"}
_COLORS = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}

# (status, label, detail)
Result = Tuple[str, str, str]


def _run(cmd: List[str], timeout: float = 10.0) -> Tuple[Optional[int], str]:
    """Run *cmd*; return (returncode, combined output). returncode None on error."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return None, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def check_python() -> Result:
    found = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] >= MIN_PYTHON:
        return OK, "Python", found
    need = ".".join(str(p) for p in MIN_PYTHON)
    return FAIL, "Python", f"{found} (need {need} or later)"


def check_docker() -> Result:
    if not shutil.which("docker"):
        return FAIL, "Docker", "docker CLI not found on PATH"
    code, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code == 0 and out:
        return OK, "Docker", f"daemon reachable (server {out.splitlines()[-1]})"
    detail = out.splitlines()[-1] if out else "no output"
    return FAIL, "Docker", f"daemon not reachable: {detail}"


def check_openshell() -> Result:
    path = shutil.which("openshell")
    if not path:
        return FAIL, "OpenShell CLI", "openshell not found on PATH (see infra/openshell/README.md)"
    code, out = _run([path, "--version"])
    match = re.search(r"\d+\.\d+\.\d+", out or "")
    if code != 0 or not match:
        return WARN, "OpenShell CLI", f"{path} (could not read version)"
    version = match.group(0)
    if version != EXPECTED_OPENSHELL:
        return WARN, "OpenShell CLI", f"{version} at {path} (expected {EXPECTED_OPENSHELL})"
    return OK, "OpenShell CLI", f"{version} at {path}"


def check_logos_home() -> Result:
    home = get_logos_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=str(home), prefix=".doctor_")
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return FAIL, "LOGOS_HOME", f"{home} is not writable: {exc}"
    return OK, "LOGOS_HOME", f"{home} is writable"


def check_auth_db() -> Result:
    db_path = get_logos_home() / "auth.db"
    if not db_path.exists():
        return OK, "Auth DB", f"{db_path} not created yet (the gateway creates it on first start)"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return FAIL, "Auth DB", f"{db_path} does not open: {exc}"
    return OK, "Auth DB", f"{db_path} opens ({tables} tables)"


CHECKS: List[Callable[[], Result]] = [
    check_python,
    check_docker,
    check_openshell,
    check_logos_home,
    check_auth_db,
]


def _format(result: Result) -> str:
    status, label, detail = result
    mark = _MARKS[status]
    if sys.stdout.isatty():
        mark = f"{_COLORS[status]}{mark}\033[0m"
    return f"  {mark} {label}: {detail}"


def run_doctor(args=None) -> int:
    """Run all checks, print a report, return the process exit code."""
    print("Logos doctor")
    results = [check() for check in CHECKS]
    for result in results:
        print(_format(result))

    failed = sum(1 for r in results if r[0] == FAIL)
    warned = sum(1 for r in results if r[0] == WARN)
    print()
    if failed:
        print(f"{failed} check(s) failed" + (f", {warned} warning(s)" if warned else ""))
        return 1
    if warned:
        print(f"All checks passed with {warned} warning(s)")
    else:
        print("All checks passed")
    return 0
