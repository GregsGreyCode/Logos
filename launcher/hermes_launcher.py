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
    """Open Logos as a standalone app window using Edge/Chrome --app mode.
    Falls back to a regular browser tab if neither is found."""
    import subprocess
    import shutil

    candidates = [
        # Windows: Edge (ships with every Win10/11 install)
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        # Windows: Chrome
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # PATH-based (macOS / Linux)
        shutil.which("google-chrome") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("chromium") or "",
        shutil.which("msedge") or "",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                subprocess.Popen([path, f"--app={_BASE_URL}", "--no-first-run"])
                return
            except Exception:
                pass
    # No Chromium-family browser found — fall back to default browser tab
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

def _ico_path() -> Path | None:
    """Locate logos.ico — works both from source tree and PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        # PyInstaller: files land in sys._MEIPASS
        p = Path(sys._MEIPASS) / "launcher" / "logos.ico"  # type: ignore[attr-defined]
        if p.exists():
            return p
    # Source tree
    p = Path(__file__).parent / "logos.ico"
    return p if p.exists() else None


def _make_icon_at_hue(hue: float):
    """Return the logos.ico resized to 64×64 with its hue rotated.

    Falls back to a plain circle if PIL ImageOps isn't available or the
    .ico file is missing.
    """
    from PIL import Image, ImageOps
    ico = _ico_path()
    if ico:
        img = Image.open(ico).convert("RGBA").resize((64, 64), Image.LANCZOS)
        # Rotate hue: convert to HSV via ImageOps is not built-in; use a fast
        # pixel-level hue shift via the 'hue' channel in HSV mode.
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        hsv = rgb.convert("HSV")
        h, s, v = hsv.split()
        # Shift each hue pixel by the desired offset (wraps in 0-255 space)
        shift = int((hue % 1.0) * 255)
        h = h.point(lambda p: (p + shift) % 256)
        shifted = Image.merge("HSV", (h, s, v)).convert("RGB")
        r2, g2, b2 = shifted.split()
        return Image.merge("RGBA", (r2, g2, b2, a))
    # Fallback: plain circle
    import colorsys
    from PIL import ImageDraw
    rv, gv, bv = colorsys.hsv_to_rgb(hue, 0.72, 0.97)
    fill = (int(rv * 255), int(gv * 255), int(bv * 255), 255)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=fill)
    return img


def _make_icon():
    try:
        return _make_icon_at_hue(0.0)  # no shift — original logo colours
    except Exception:
        return None


def _start_icon_animation(icon) -> None:
    """Cycle the tray icon hue — mirrors the logo CSS hue-rotate animation."""
    _HUE_STEP = 1.0 / 80   # full cycle in ~8 s at 100 ms ticks
    hue = 0.0

    def _loop():
        nonlocal hue
        while True:
            try:
                icon.icon = _make_icon_at_hue(hue)
            except Exception:
                pass
            hue = (hue + _HUE_STEP) % 1.0
            time.sleep(0.1)

    threading.Thread(target=_loop, daemon=True, name="logos-icon-anim").start()


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
    _start_icon_animation(icon)
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
