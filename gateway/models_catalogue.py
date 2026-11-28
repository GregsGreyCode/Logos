"""A copy of models.dev's public AI-model catalogue, kept in the house for the agents (E6 follow-up).

hermes (the agent software) downloads ``https://models.dev/api.json`` when it starts with no copy of
its own and refreshes it every four hours (``/opt/hermes/agent/models_dev.py``: an ETag ``GET``, kept
in ``$HERMES_HOME/models_dev_cache.json``). It uses it for model names, context sizes and prices. A
sandbox cannot reach the internet, so every refresh was refused and became a "wants to reach
models.dev" question for each agent's people (28 Nov: three at once after a restart).

So Logos keeps a copy and hermes is pointed at it (its supported mirror setting, ``models_dev.url``
in config.yaml, written by ``gateway/agent_runtime.py``):

* :func:`refresh` downloads it **on the host**, only when Logos is deployed or updated
  (``scripts/logos-deploy``; by hand: ``python3 gateway/models_catalogue.py refresh``). It is checked
  (valid JSON, a sane size, enough providers and models) and written atomically; a failed or odd
  download keeps the last good copy.
* The dashboard serves it read-only at :data:`PATH` (no login: it is public data, and the sandbox
  rule ``models_catalogue`` lets an agent reach that one path with ``GET`` and nothing else).
* Nothing leaves the sandbox, nobody is asked, and hermes has the same data as before.

Standard library only: ``scripts/logos-deploy`` and ``scripts/logos-agent`` load it with the system
Python (aiohttp only inside :func:`register`).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE = "https://models.dev/api.json"
PATH = "/api/v2/models-dev/api.json"         # on the dashboard; the only path the sandbox rule opens
FILE = "models-dev.json"                     # in ~/.logos, beside the deploy's own state
MIN_BYTES = 100_000                          # 28 Nov: 6.9 MB, 327 providers, 10,076 models
MAX_BYTES = 64_000_000
MIN_PROVIDERS = 20
MIN_MODELS = 200
TIMEOUT_S = 60


def store(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("HOME") or str(Path.home())) / ".logos" / FILE


def check(raw: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(catalogue, "")`` when ``raw`` looks like models.dev's catalogue, else ``(None, why)``."""
    if not MIN_BYTES <= len(raw) <= MAX_BYTES:
        return None, f"unexpected size ({len(raw)} bytes)"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "not valid JSON"
    if not isinstance(data, dict):
        return None, "not a catalogue (not a JSON object)"
    providers = [p for p in data.values() if isinstance(p, dict) and isinstance(p.get("models"), dict)]
    models = sum(len(p["models"]) for p in providers)
    if len(providers) < MIN_PROVIDERS or models < MIN_MODELS:
        return None, f"too small ({len(providers)} providers, {models} models)"
    return data, ""


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "logos-deploy", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        raw = r.read(MAX_BYTES + 1)
    return raw


def refresh(env: Optional[Mapping[str, str]] = None, fetch: Callable[[str], bytes] = _download,
            url: str = SOURCE) -> Tuple[bool, str]:
    """Download a fresh copy on the host. ``(True, summary)`` when it was replaced; ``(False, why)``
    when not, the last good copy (if any) staying as it was. Never raises."""
    path = store(env)
    try:
        raw = fetch(url)
    except Exception as e:
        return False, f"could not download it ({type(e).__name__}); kept the last copy" if path.exists() \
            else f"could not download it ({type(e).__name__}); no copy yet"
    data, why = check(raw)
    if data is None:
        return False, f"the download was refused: {why}; kept the last copy" if path.exists() \
            else f"the download was refused: {why}; no copy yet"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except OSError as e:
        return False, f"could not be saved ({e})"
    models = sum(len(p.get("models") or {}) for p in data.values() if isinstance(p, dict))
    return True, f"{len(data)} providers, {models} models, {len(raw) // 1000} kB"


class Served:
    """The copy as the dashboard serves it: read once, again only when the file changes."""

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        self.env = env
        self._key: Optional[Tuple[float, int]] = None
        self._body = b""
        self._gz = b""
        self._etag = ""

    def get(self) -> Tuple[bytes, bytes, str]:
        """``(body, gzipped body, etag)``, or empty when there is no good copy. Blocking (it reads and
        compresses the file when it changed): call it in a thread."""
        path = store(self.env)
        try:
            st = path.stat()
        except OSError:
            return b"", b"", ""
        key = (st.st_mtime, st.st_size)
        if key != self._key:
            raw = path.read_bytes()
            if check(raw)[0] is None:           # a copy edited or cut short by hand is not served
                return b"", b"", ""
            self._key, self._body, self._gz = key, raw, gzip.compress(raw, 6)
            # models.dev's own ETag is the same: the first 32 hex digits of the SHA-256.
            self._etag = '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'
        return self._body, self._gz, self._etag


def register(app: Any, env: Optional[Mapping[str, str]] = None) -> None:
    from aiohttp import web
    served = Served(env)

    async def handle(request: "web.Request") -> "web.StreamResponse":
        body, gz, etag = await asyncio.to_thread(served.get)
        if not body:
            return web.json_response({"error": "no_copy", "message": "No copy yet: it is fetched when Logos is deployed."},
                                     status=404)
        if etag in (request.headers.get("If-None-Match") or ""):
            return web.Response(status=304, headers={"ETag": etag})
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if "gzip" in (request.headers.get("Accept-Encoding") or ""):
            return web.Response(body=gz, content_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
        return web.Response(body=body, content_type="application/json", headers=headers)

    app.router.add_get(PATH, handle)


if __name__ == "__main__":
    if sys.argv[1:] != ["refresh"]:
        print("usage: python3 gateway/models_catalogue.py refresh", file=sys.stderr)
        sys.exit(2)
    ok, msg = refresh()
    print(f"models.dev catalogue: {'updated' if ok else 'not updated'}: {msg} ({store()})")
    sys.exit(0 if ok else 1)
