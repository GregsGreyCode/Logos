"""The Updates screen: what is new on v2 for this machine, and the "Update now" button.

The work is done by ``scripts/logos-deploy``. The button starts it as its own systemd user
unit, so it carries on while it restarts this dashboard. The screen then reads the
progress the script writes to ``~/.logos/deploy-status.json`` and its output in
``~/.logos/deploy-output.txt``.

Admins only: an update restarts agents and runs code from the repository.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "logos-deploy"
UNIT = "logos-deploy"
STATE_DIR = Path.home() / ".logos"
STATUS = STATE_DIR / "deploy-status.json"
OUTPUT = STATE_DIR / "deploy-output.txt"
PLAN_CACHE_S = 60
OUTPUT_LINES = 200


def _run(argv: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO, text=True, capture_output=True, timeout=timeout)


def unit_active() -> bool:
    return _run(["systemctl", "--user", "is-active", "--quiet", UNIT], timeout=10).returncode == 0


def read_status() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None


def read_output() -> List[str]:
    try:
        return OUTPUT.read_text(errors="replace").splitlines()[-OUTPUT_LINES:]
    except OSError:
        return []


def check_plan() -> Dict[str, Any]:
    """Run ``logos-deploy --plan-json`` (fetches origin, changes nothing)."""
    try:
        r = _run([sys.executable, str(SCRIPT), "--plan-json"], timeout=90)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
        return {"refused": f"Could not check for updates: {e}"}


def start_argv() -> List[str]:
    """The transient unit that runs the update, outliving a restart of this service."""
    return ["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
            f"--working-directory={REPO}",
            f"--setenv=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "--setenv=PYTHONUNBUFFERED=1",
            f"--property=StandardOutput=truncate:{OUTPUT}", "--property=StandardError=inherit",
            sys.executable, str(SCRIPT), "--yes"]


def start() -> Optional[str]:
    """Start the update. Returns an error message, or None when it started."""
    if unit_active():
        return "An update is already running."
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    r = _run(start_argv(), timeout=20)
    if r.returncode != 0:
        return (r.stderr or r.stdout or "systemd-run failed").strip()
    return None


def _admin(request: web.Request) -> bool:
    from gateway.dashboard_api import _user
    return _user(request)["role"] == "admin"


async def handle_get(request: web.Request) -> web.Response:
    """GET /api/v2/updates[?check=1] — progress of the last update, and with ``check`` the
    plan for the next one (cached for a minute; checking runs ``git fetch``)."""
    if not _admin(request):
        return web.json_response({"error": "forbidden"}, status=403)
    running = await asyncio.to_thread(unit_active)
    out: Dict[str, Any] = {"running": running, "status": await asyncio.to_thread(read_status),
                           "output": await asyncio.to_thread(read_output)}
    if request.query.get("check") and not running:
        loop = asyncio.get_running_loop()
        cached = request.app.get("updates_plan")
        if request.query.get("fresh") or not cached or loop.time() - cached[0] > PLAN_CACHE_S:
            cached = (loop.time(), await asyncio.to_thread(check_plan))
            request.app["updates_plan"] = cached
        out["plan"] = cached[1]
    return web.json_response(out)


async def handle_start(request: web.Request) -> web.Response:
    """POST /api/v2/updates — start the update in the background."""
    if not _admin(request):
        return web.json_response({"error": "forbidden"}, status=403)
    error = await asyncio.to_thread(start)
    if error:
        return web.json_response({"error": error}, status=409)
    request.app.pop("updates_plan", None)
    logger.info("updates: started by %s", (request.get("current_user") or {}).get("username"))
    return web.json_response({"started": True})


# The reminder (Greg, 23 Nov): whoever manages Logos sees "An update is ready" in Notifications, with a
# button to Settings › Updates. Checked in the background at most every few hours, so Notifications never
# waits on a git fetch; the Updates screen's own check (a minute's cache) also counts.
REMIND_EVERY_S = 6 * 3600


async def _refresh_plan(app: web.Application) -> None:
    try:
        plan = await asyncio.to_thread(check_plan)
        app["updates_plan"] = (asyncio.get_running_loop().time(), plan)
    except Exception:                                    # pragma: no cover - checked again later
        logger.warning("updates: background check failed", exc_info=True)
    finally:
        app.pop("updates_checking", None)


def reminder(app: web.Application) -> Optional[Dict[str, Any]]:
    """A ``needs`` notification when there is something to install, from what was last checked; starts
    a check in the background when that is old. None when up to date, unknown or refused."""
    loop = asyncio.get_running_loop()
    cached = app.get("updates_plan")
    if (not cached or loop.time() - cached[0] > REMIND_EVERY_S) and not app.get("updates_checking"):
        app["updates_checking"] = asyncio.ensure_future(_refresh_plan(app))
    plan = cached[1] if cached else None
    commits = (plan or {}).get("commits") or []
    if not commits or (plan or {}).get("refused"):
        return None
    newest = str(commits[0].get("sha") or "")
    first_seen = app.setdefault("updates_first_seen", {}).setdefault(newest, time.time())
    n = len(commits)
    return {"id": f"logos-update:{newest}", "kind": "logos-update", "type": "needs", "ts": first_seen,
            "title": "An update for Logos is ready",
            "text": f"{n} change{'' if n == 1 else 's'} to install, the newest: {commits[0].get('subject') or ''}".strip(),
            "link": "#/settings/updates"}


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/updates", handle_get)
    app.router.add_post("/api/v2/updates", require_csrf(handle_start))
