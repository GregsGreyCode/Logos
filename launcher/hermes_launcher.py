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

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PORT = int(os.environ.get("LOGOS_PORT", "8080"))
_BASE_URL = f"http://127.0.0.1:{_PORT}"
_HEALTH_URL = f"{_BASE_URL}/health"
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_LOG_PATH = _HERMES_HOME / "logs" / "launcher.log"

# ---------------------------------------------------------------------------
# Gateway process management
# ---------------------------------------------------------------------------

_gateway_proc: subprocess.Popen | None = None
_gateway_lock = threading.Lock()


def _find_gateway_cmd() -> list[str]:
    """Locate the gateway entry point — works both from source and PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle: use the bundled hermes script
        base = Path(sys.executable).parent
        hermes_bin = base / ("hermes.exe" if sys.platform == "win32" else "hermes")
        if hermes_bin.exists():
            return [str(hermes_bin), "gateway", "run", "--port", str(_PORT)]
    # Source or installed package
    return [sys.executable, "-m", "gateway.run", "--port", str(_PORT)]


def _start_gateway() -> None:
    global _gateway_proc
    with _gateway_lock:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HERMES_RUNTIME_MODE": "local"}
        with open(_LOG_PATH, "a") as log_fh:
            _gateway_proc = subprocess.Popen(
                _find_gateway_cmd(),
                env=env,
                stdout=log_fh,
                stderr=log_fh,
            )


def _stop_gateway() -> None:
    global _gateway_proc
    with _gateway_lock:
        if _gateway_proc and _gateway_proc.poll() is None:
            _gateway_proc.terminate()
            try:
                _gateway_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _gateway_proc.kill()
        _gateway_proc = None


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


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

def _make_icon():
    """Create a simple coloured square icon (placeholder — replace with logo.png)."""
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
        # pystray needs an image; fall back to a 1×1 transparent PNG
        from PIL import Image
        img = Image.new("RGBA", (1, 1))

    icon = pystray.Icon("Logos", img, "Logos", menu=None)
    icon.menu = _build_menu(icon)
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Start gateway in background
    _start_gateway()

    # Open browser once gateway is ready
    def _open_when_ready():
        if _wait_for_gateway():
            _open_browser()
        else:
            print("[launcher] Gateway did not start in time — check logs at", _LOG_PATH)

    threading.Thread(target=_open_when_ready, daemon=True).start()

    # Run tray icon (blocks until Quit)
    try:
        _run_tray()
    except ImportError:
        # pystray not installed — headless mode (useful on Linux servers)
        print(f"[launcher] Logos running at {_BASE_URL} (no tray icon)")
        try:
            _gateway_proc.wait()
        except (KeyboardInterrupt, AttributeError):
            pass
        finally:
            _stop_gateway()


if __name__ == "__main__":
    main()
