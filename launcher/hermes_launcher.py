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
import http.server
import multiprocessing
import os
import socketserver
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
# Splash server — serves a branded loading page on a separate port while the
# gateway starts up, so the --app window never shows Edge's error page.
_SPLASH_PORT = int(os.environ.get("LOGOS_SPLASH_PORT", "8079"))
_SPLASH_URL = f"http://127.0.0.1:{_SPLASH_PORT}"

# Inline loading page — polls gateway health and redirects when ready.
# Uses /favicon.svg served by the same splash handler so Edge shows the
# Logos icon in the title bar instead of the browser globe.
_SPLASH_HTML = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Logos</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;background:#0d0d0d;display:flex;align-items:center;
  justify-content:center;font-family:-apple-system,BlinkMacSystemFont,
  "Segoe UI",system-ui,sans-serif;color:#fff}}
.card{{text-align:center;padding:48px 40px}}
.ring{{width:72px;height:72px;border-radius:50%;border:3px solid transparent;
  border-top-color:#7c3aed;border-right-color:#2563eb;
  animation:spin 1.1s linear infinite;margin:0 auto 28px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
h1{{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-bottom:8px}}
p{{font-size:14px;color:#666;letter-spacing:.02em}}
p.err{{color:#ef4444;margin-top:16px;font-size:13px;line-height:1.5}}
</style>
</head>
<body>
<div class="card">
  <div class="ring" id="ring"></div>
  <h1>Logos</h1>
  <p id="msg">Starting up&hellip;</p>
</div>
<script>
var deadline = Date.now() + 90000;
(function poll(){{
  if(Date.now() > deadline){{
    document.getElementById("ring").style.animationPlayState="paused";
    document.getElementById("msg").className="err";
    document.getElementById("msg").innerHTML=
      "Logos did not start in time.<br>Check logs at %USERPROFILE%\\.hermes\\logs\\launcher.log";
    return;
  }}
  fetch("http://127.0.0.1:{_PORT}/health")
    .then(function(r){{if(r.ok){{location.href="http://127.0.0.1:{_PORT}";return;}}setTimeout(poll,600);
    }}).catch(function(){{setTimeout(poll,600);}});
}})();
</script>
</body>
</html>
""".encode()


def _logo_svg_bytes() -> bytes:
    """Return the Logos SVG icon, resolved from the bundle or source tree."""
    if getattr(sys, "frozen", False):
        p = Path(sys._MEIPASS) / "assets" / "logo.svg"  # type: ignore[attr-defined]
    else:
        p = Path(__file__).parent.parent / "assets" / "logo.svg"
    try:
        return p.read_bytes()
    except OSError:
        # Minimal fallback SVG if the asset is missing
        return (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            b'<circle cx="50" cy="50" r="45" fill="#7c3aed"/>'
            b"</svg>"
        )

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
            # replace=True clears any stale PID file from a previous crashed
            # or force-killed instance — without this the gateway refuses to
            # start if the PID file exists but the process is gone.
            loop.run_until_complete(start_gateway(None, replace=True))
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
    _gateway_ready.clear()  # go back to colour-cycling during restart
    _stop_gateway()
    time.sleep(0.5)
    _start_gateway()
    _wait_for_login(timeout=60)
    _gateway_ready.set()


def _wait_for_login(timeout: int = 60) -> bool:
    """Poll /login until the full UI is serving — used to gate the tray colour."""
    import urllib.request
    deadline = time.monotonic() + timeout
    url = f"{_BASE_URL}/login"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


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


# ---------------------------------------------------------------------------
# Splash server — branded loading page while gateway starts up
# ---------------------------------------------------------------------------

class _SplashHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/favicon.svg":
            data = _logo_svg_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_SPLASH_HTML)))
            self.end_headers()
            self.wfile.write(_SPLASH_HTML)

    def log_message(self, *args):
        pass  # suppress console noise


def _start_splash() -> socketserver.TCPServer | None:
    """Start the splash HTTP server. Returns the server (call .shutdown() when done)."""
    try:
        server = socketserver.TCPServer(("127.0.0.1", _SPLASH_PORT), _SplashHandler)
        server.allow_reuse_address = True
        threading.Thread(target=server.serve_forever, daemon=True, name="logos-splash").start()
        return server
    except OSError:
        return None  # port in use — skip splash, fall back to direct URL


def _open_browser(url: str = _BASE_URL) -> None:
    """Open Logos as a standalone app window using Edge/Chrome --app mode.
    Falls back to a regular browser tab if neither is found.

    A dedicated --user-data-dir is passed so Logos gets its own browser
    profile, isolated from the user's main Edge/Chrome session. This also
    fixes the 'tray → Open Logos does nothing after closing the window' bug:
    without a separate profile, Edge detects an existing instance is already
    running and silently ignores the new --app launch instead of opening a
    fresh window.
    """
    import subprocess
    import shutil

    # Logos-specific browser profile — keeps session data out of the user's
    # main browser and guarantees a new window is always opened on demand.
    _profile_dir = str(_HERMES_HOME / "browser-profile")

    candidates = [
        # Windows: Edge (ships with every Win10/11 install — check both common locations)
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        # Windows: Edge in user-local install (newer installations)
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
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
                subprocess.Popen([
                    path,
                    f"--app={url}",
                    f"--user-data-dir={_profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1280,800",
                ])
                return
            except Exception:
                pass
    # No Chromium-family browser found — fall back to default browser tab
    webbrowser.open(url)


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
        img = Image.open(ico).convert("RGBA")
        # Auto-crop to the tight bounding box of non-transparent pixels so the
        # logo mark fills the icon area rather than sitting in a sea of padding.
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img = img.resize((64, 64), Image.LANCZOS)
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


# Set when the gateway is ready — animation thread watches this to switch
# from colour-cycling (loading) to colourless/static (running).
_gateway_ready = threading.Event()


def _make_icon_greyscale():
    """Return the logo icon desaturated — used when Logos is fully running."""
    from PIL import Image, ImageOps
    ico = _ico_path()
    if ico:
        img = Image.open(ico).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img = img.resize((64, 64), Image.LANCZOS)
        r, g, b, a = img.split()
        grey = ImageOps.grayscale(Image.merge("RGB", (r, g, b)))
        grey_rgba = Image.merge("RGBA", (*grey.split() * 3, a))
        return grey_rgba
    # Fallback: grey circle
    from PIL import ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=(160, 160, 160, 255))
    return img


def _start_icon_animation(icon) -> None:
    """Cycle the tray icon hue while loading; go colourless once gateway is ready."""
    _HUE_STEP = 1.0 / 80   # full colour cycle in ~8 s at 100 ms ticks
    hue = 0.0

    def _loop():
        nonlocal hue
        while True:
            if _gateway_ready.is_set():
                # Connected — cycle colours
                try:
                    icon.icon = _make_icon_at_hue(hue)
                except Exception:
                    pass
                hue = (hue + _HUE_STEP) % 1.0
                time.sleep(0.1)
            else:
                # Loading — greyscale/static
                try:
                    icon.icon = _make_icon_greyscale()
                except Exception:
                    pass
                # Poll every 200 ms until ready, then start cycling
                _gateway_ready.wait(timeout=0.2)

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

    icon = pystray.Icon("Logos", img, "Logos - Agentic AI Platform", menu=None)
    icon.menu = _build_menu(icon)
    _start_icon_animation(icon)
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _start_gateway()

    def _open_when_ready():
        # Start the branded splash page immediately so the --app window never
        # shows Edge's "can't reach this page" error while the gateway boots.
        splash = _start_splash()
        if splash:
            _open_browser(_SPLASH_URL)
            # The splash page JS will redirect itself to the gateway when
            # /health returns 200 — we just need to wait and then clean up.
            if _wait_for_gateway(timeout=60):
                # Give the redirect a moment to fire before shutting down.
                time.sleep(1.5)
                splash.shutdown()
                # Now wait for /login — that's when the full UI is ready
                # and the tray icon switches from colour-cycling to colour.
                _wait_for_login(timeout=30)
                _gateway_ready.set()
            else:
                _log("Gateway did not start in time — check logs at " + str(_LOG_PATH))
                # Open directly so the user isn't stuck on the loading screen.
                _open_browser(_BASE_URL)
                splash.shutdown()
        else:
            # Splash port was busy — fall back to opening the gateway directly.
            if _wait_for_gateway(timeout=60):
                _open_browser()
                _wait_for_login(timeout=30)
                _gateway_ready.set()
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
