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
import json
import multiprocessing
import os
import socketserver
import sys
import threading
import time
import urllib.request
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
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".logos"))
# Pin into os.environ so the in-process gateway import sees the same path.
os.environ.setdefault("HERMES_HOME", str(_HERMES_HOME))
_CONNECT_JSON = _HERMES_HOME / "connect.json"
_LOG_PATH = _HERMES_HOME / "logs" / "logos.log"
_UPDATES_DIR = _HERMES_HOME / "updates"

# Current app version — read from bundled pyproject.toml, or importlib.metadata
try:
    import tomllib as _tomllib_v
    if getattr(sys, "frozen", False):
        _pyproj = Path(sys._MEIPASS) / "pyproject.toml"  # type: ignore[attr-defined]
    else:
        _pyproj = Path(__file__).parent.parent / "pyproject.toml"
    with open(_pyproj, "rb") as _fv:
        _APP_VERSION = _tomllib_v.load(_fv)["project"]["version"]
except Exception:
    try:
        import importlib.metadata as _imeta
        _APP_VERSION = _imeta.version("logos")
    except Exception:
        _APP_VERSION = "0.0.0"

_GITHUB_RELEASES_API = (
    "https://api.github.com/repos/GregsGreyCode/Logos/releases/latest"
)
_GITHUB_RELEASES_PAGE = (
    "https://github.com/GregsGreyCode/Logos/releases/latest"
)
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
      "Logos did not start in time.<br>Check logs at {str(_LOG_PATH).replace(chr(92), '/')}";
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
                    "--disable-sync",
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
# Auto-update — Plan C: tray-driven background updater
# ---------------------------------------------------------------------------

class _Upd:
    """Thread-safe update state.  All fields written under .lock."""
    lock = threading.Lock()
    available: str = ""           # "" = none known; "0.4.17" = update ready to offer
    download_url: str = ""        # HTTPS URL to the LogosSetup-*.exe asset
    downloading: bool = False     # True while the .exe is streaming to disk
    ready_path: Path | None = None  # set once the download is complete


def _parse_version(v: str) -> tuple[int, ...]:
    """Convert "v0.4.17" or "0.4.17" to (0, 4, 17) for comparison."""
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def _check_for_update() -> tuple[str, str] | tuple[None, None]:
    """Query GitHub releases API.  Returns (version_str, action_url) or (None, None).

    action_url on Windows: direct .exe installer download URL (if asset exists).
    action_url on Linux/macOS: GitHub releases page URL (opens in browser).
    In both cases a non-None return means a newer version exists.
    """
    try:
        req = urllib.request.Request(
            _GITHUB_RELEASES_API,
            headers={"User-Agent": f"Logos/{_APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        tag = data.get("tag_name", "")
        if not tag:
            return None, None
        if _parse_version(tag) <= _parse_version(_APP_VERSION):
            return None, None  # already up to date
        version = tag.lstrip("v")
        if sys.platform == "win32":
            # Prefer direct installer download on Windows
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.startswith("LogosSetup") and name.endswith(".exe"):
                    return version, asset["browser_download_url"]
        # Non-Windows, or Windows build not yet published: fall back to releases page
        return version, data.get("html_url", _GITHUB_RELEASES_PAGE)
    except Exception as exc:
        _log(f"Update check failed: {exc}")
    return None, None


def _download_update(version: str, url: str, icon) -> Path | None:
    """Download installer to ~/.logos/updates/.  Returns local path or None."""
    _UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _UPDATES_DIR / f"LogosSetup-{version}.exe"
    if dest.exists():
        return dest  # already cached from a previous check
    tmp = dest.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
        return dest
    except Exception as exc:
        _log(f"Update download failed: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _apply_update(path: Path, icon) -> None:
    """Launch the installer silently then exit so the file lock is released.

    The installer is started with DETACHED_PROCESS so it keeps running after
    the launcher exits.  /SILENT shows a progress window so the user knows
    something is happening.  A WizardSilent [Run] entry in logos.iss ensures
    Logos relaunches automatically once the install finishes.
    """
    import subprocess
    try:
        icon.notify("Installing Logos update — this will take a moment.", "Logos Update")
        time.sleep(1.5)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS
        subprocess.Popen([str(path), "/SILENT", "/NORESTART"], **kwargs)
        time.sleep(0.5)
    except Exception as exc:
        _log(f"Update install failed: {exc}")
        icon.notify("Update failed — see logs.", "Logos Update")
        return
    _stop_gateway()
    icon.stop()


def _start_update_checker(icon) -> None:
    """Background daemon: check for updates 30 s after start, then every 24 h."""

    def _loop():
        time.sleep(30)
        while True:
            version, url = _check_for_update()
            if version and url:
                with _Upd.lock:
                    _Upd.available = version
                    _Upd.download_url = url
                _log(f"Update available: v{version}")
                icon.notify(
                    f"Logos {version} is available. Open the tray menu to update.",
                    "Update Available",
                )
                _rebuild_menu(icon)
            time.sleep(24 * 3600)

    threading.Thread(target=_loop, daemon=True, name="logos-update-check").start()


def _rebuild_menu(icon) -> None:
    """Reassemble the tray menu, injecting an update item when one is available."""
    import pystray

    with _Upd.lock:
        avail = _Upd.available
        downloading = _Upd.downloading
        ready = _Upd.ready_path
        url = _Upd.download_url

    # --- update section (injected at top of menu) ---
    extra: list = []
    if ready and avail:
        def _on_install(icon, item, _p=ready, _v=avail):
            threading.Thread(
                target=_apply_update, args=(_p, icon), daemon=True, name="logos-update-apply"
            ).start()
        extra = [
            pystray.MenuItem(f"Install Logos {avail} & restart", _on_install),
            pystray.Menu.SEPARATOR,
        ]
    elif downloading:
        extra = [
            pystray.MenuItem("Downloading update\u2026", None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
    elif avail and url:
        _is_installer = url.endswith(".exe")

        if _is_installer:
            # Windows: download the installer then offer to run it
            def _on_download(icon, item, _v=avail, _u=url):
                with _Upd.lock:
                    _Upd.downloading = True
                _rebuild_menu(icon)

                def _dl(_v=_v, _u=_u):
                    p = _download_update(_v, _u, icon)
                    with _Upd.lock:
                        _Upd.downloading = False
                        _Upd.ready_path = p
                    if p:
                        icon.notify(
                            f"Logos {_v} ready. Click 'Install' in the tray menu.",
                            "Logos Update",
                        )
                    _rebuild_menu(icon)

                threading.Thread(target=_dl, daemon=True, name="logos-update-dl").start()

            extra = [
                pystray.MenuItem(f"Update to Logos {avail}\u2026", _on_download),
                pystray.Menu.SEPARATOR,
            ]
        else:
            # Linux/macOS: open the releases page in the system browser
            def _on_view(icon, item, _u=url):
                webbrowser.open(_u)

            extra = [
                pystray.MenuItem(f"Logos {avail} available \u2014 view release", _on_view),
                pystray.Menu.SEPARATOR,
            ]

    # --- standard items ---
    def on_open(icon, item):
        _open_browser()

    def on_restart(icon, item):
        icon.notify("Restarting Logos\u2026", "Logos")
        threading.Thread(target=_restart_gateway, daemon=True).start()

    def on_quit(icon, item):
        _stop_gateway()
        icon.stop()

    icon.menu = pystray.Menu(
        *extra,
        pystray.MenuItem("Open Logos", on_open, default=True),
        pystray.MenuItem("Restart", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )


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
    """Cycle the tray icon hue in phase-lock with the browser UI (6 deg/s = 60 s/cycle)."""

    def _fetch_epoch() -> float:
        """Query the gateway for the shared hue epoch so tray stays phase-locked."""
        import urllib.request as _ur
        import json as _js
        try:
            with _ur.urlopen("http://127.0.0.1:4444/api/hue", timeout=2) as r:
                return _js.loads(r.read())["epoch_ms"] / 1000.0
        except Exception:
            return time.time()

    def _hue_from_epoch(epoch: float) -> float:
        """Compute current hue (0–1) from the shared epoch, matching the browser formula."""
        return (((time.time() - epoch) * 6) % 360) / 360.0

    def _loop():
        epoch: float | None = None
        while True:
            if _gateway_ready.is_set():
                if epoch is None:
                    epoch = _fetch_epoch()
                try:
                    icon.icon = _make_icon_at_hue(_hue_from_epoch(epoch))
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                epoch = None  # reset so we re-sync phase when gateway comes back
                try:
                    icon.icon = _make_icon_greyscale()
                except Exception:
                    pass
                _gateway_ready.wait(timeout=0.2)

    threading.Thread(target=_loop, daemon=True, name="logos-icon-anim").start()


def _run_tray() -> None:
    import pystray

    img = _make_icon()
    if img is None:
        from PIL import Image
        img = Image.new("RGBA", (1, 1))

    icon = pystray.Icon("Logos", img, "Logos - Agentic AI Platform", menu=None)
    _rebuild_menu(icon)
    _start_icon_animation(icon)
    _start_update_checker(icon)
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _read_connect_url() -> str | None:
    """Return the remote Logos URL from connect.json, or None if not set."""
    try:
        if _CONNECT_JSON.exists():
            data = json.loads(_CONNECT_JSON.read_text(encoding="utf-8"))
            url = (data.get("url") or "").strip().rstrip("/")
            return url or None
    except Exception:
        pass
    return None


def _run_tray_client_mode(remote_url: str) -> None:
    """Tray-only mode: no local gateway, just a launcher for a remote Logos server."""
    import pystray

    img = _make_icon()
    if img is None:
        from PIL import Image
        img = Image.new("RGBA", (1, 1))

    def on_open(icon, item):
        webbrowser.open(remote_url)

    def on_switch_local(icon, item):
        try:
            _CONNECT_JSON.unlink(missing_ok=True)
        except Exception:
            pass
        icon.notify("Switched to local mode. Restart Logos to set up a local server.", "Logos")
        icon.stop()

    def on_quit(icon, item):
        icon.stop()

    icon = pystray.Icon("Logos", img, f"Logos — {remote_url}", menu=pystray.Menu(
        pystray.MenuItem("Open Logos", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Switch to local mode…", on_switch_local),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    ))
    icon.run()


def main() -> None:
    remote_url = _read_connect_url()
    if remote_url:
        _log(f"Client mode: opening remote Logos at {remote_url}")
        webbrowser.open(remote_url)
        try:
            _run_tray_client_mode(remote_url)
        except ImportError:
            print(f"[launcher] Client mode — Logos at {remote_url} (no tray icon)")
        return

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
