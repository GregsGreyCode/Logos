"""
Logos Desktop Launcher — system tray application for Windows (and Linux/macOS).

Starts the Logos gateway server in local mode, opens the browser on first run,
and provides a tray icon with Open / Restart / Quit options.

Build with PyInstaller:
    pyinstaller launcher/hermes_launcher.spec

Requirements (desktop only, not added to main pyproject.toml):
    pystray>=0.19
    Pillow>=10.0
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# PyInstaller / multiprocessing guard — MUST be first executable statement.
# On Windows, frozen executables re-run the entry point for every spawned
# process (no fork). freeze_support() detects that case and exits early
# so only the real launcher proceeds.
# ---------------------------------------------------------------------------
multiprocessing.freeze_support()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PORT = int(os.environ.get("LOGOS_PORT", "8080"))
_BASE_URL = f"http://127.0.0.1:{_PORT}"
_HEALTH_URL = f"{_BASE_URL}/health"
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_LOG_PATH = _HERMES_HOME / "logs" / "launcher.log"

# ---------------------------------------------------------------------------
# Gateway — runs in-process in a background thread.
# This avoids the PyInstaller Windows re-entry problem: when frozen,
# sys.executable IS Logos.exe, so subprocess.Popen([sys.executable, ...])
# would re-run the launcher, spawning infinite processes.
# ---------------------------------------------------------------------------

_gateway_loop: asyncio.AbstractEventLoop | None = None
_gateway_thread: threading.Thread | None = None
_gateway_lock = threading.Lock()


def _start_gateway() -> None:
    global _gateway_loop, _gateway_thread

    os.environ.setdefault("HERMES_RUNTIME_MODE", "local")
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _run() -> None:
        global _gateway_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with _gateway_lock:
            _gateway_loop = loop
        try:
            from gateway.run import start_gateway  # type: ignore
            loop.run_until_complete(start_gateway(None))
        except Exception as exc:
            _log(f"Gateway error: {exc}")
        finally:
            loop.close()
            with _gateway_lock:
                _gateway_loop = None

    _gateway_thread = threading.Thread(target=_run, daemon=True, name="logos-gateway")
    _gateway_thread.start()


def _stop_gateway() -> None:
    with _gateway_lock:
        loop = _gateway_loop
    if loop and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    if _gateway_thread:
        _gateway_thread.join(timeout=5)


def _restart_gateway() -> None:
    _stop_gateway()
    time.sleep(0.5)
    _start_gateway()
    _wait_for_gateway()


def _wait_for_gateway(timeout: int = 20) -> bool:
    """Poll /health until the gateway is accepting requests."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _open_browser() -> None:
    webbrowser.open(_BASE_URL)


def _log(msg: str) -> None:
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

def _make_icon():
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(99, 102, 241, 255))  # indigo
        return img
    except ImportError:
        return None


def _build_menu(icon):
    import pystray

    def on_open(icon, item):
        _open_browser()

    def on_restart(icon, item):
        icon.notify("Restarting Logos…", "Logos")
        threading.Thread(target=_restart_gateway, daemon=True).start()

    def on_quit(icon, item):
        _stop_gateway()
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem("Open Logos", on_open, default=True),
        pystray.MenuItem("Restart", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )


def _run_tray() -> None:
    import pystray

    img = _make_icon()
    if img is None:
        from PIL import Image
        img = Image.new("RGBA", (1, 1))

    icon = pystray.Icon("Logos", img, "Logos", menu=None)
    icon.menu = _build_menu(icon)
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _start_gateway()

    def _open_when_ready():
        if _wait_for_gateway():
            _open_browser()
        else:
            _log("Gateway did not start in time — check logs at " + str(_LOG_PATH))

    threading.Thread(target=_open_when_ready, daemon=True).start()

    try:
        _run_tray()
    except ImportError:
        # pystray not installed — headless mode (useful on Linux servers)
        print(f"[launcher] Logos running at {_BASE_URL} (no tray icon)")
        try:
            while _gateway_thread and _gateway_thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            _stop_gateway()


if __name__ == "__main__":
    main()
