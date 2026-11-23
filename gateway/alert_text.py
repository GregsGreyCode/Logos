"""What an agent said, for the phone alert (Greg, 23 Nov: option 1 of "who and what").

Matrix messages are end-to-end encrypted, so the alert the Matrix server asks Logos to send
(push.py) knows who wrote but not what. Agents run on this server, so their replies are written
here anyway: each agent's ``logos-alert-text`` plugin hands Logos the first words of a reply as
it sends it (``POST /api/v2/alert-text``), and the alert shows them.

- Kept in memory only, for two minutes, then forgotten: never on disk, never logged.
- Each agent signs with its own token, an HMAC of its name under a key only Logos holds
  (``~/.config/logos/alert-text.key``, 0600), so Logos checks it without reading the agent's files.
- An agent can only describe its own messages: the alert uses the words only when the Matrix
  server says the message was sent by ``@<that agent>-agent``.
- The sandbox may reach only this one path on the dashboard (its network policy, ``alerts``).
- People's own messages stay "New message": only their phones can read them (option 2, docs/push.md).
- Whoever chose private alerts in Settings never gets the words.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from aiohttp import web

PATH = "/api/v2/alert-text"
TOKEN_ENV = "LOGOS_ALERT_TOKEN"
URL_ENV = "LOGOS_ALERT_URL"
KEY_FILE = "alert-text.key"
KEEP_S = 120                     # long enough for the Matrix server's push; then forgotten
MAX_KEPT = 500
SHOWN = 180                      # characters in the alert; a phone shows two or three lines
WAIT_S = 2.5                     # the push can arrive just before the agent's note does


def _config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def key(env: Optional[Mapping[str, str]] = None) -> bytes:
    """Made once, kept beside Logos's other keys, owner-only."""
    path = _config_dir(env if env is not None else os.environ) / KEY_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip().encode()


def token_for(agent: str, env: Optional[Mapping[str, str]] = None) -> str:
    return hmac.new(key(env), f"alert-text:{agent}".encode(), hashlib.sha256).hexdigest()


def valid(agent: str, token: str, env: Optional[Mapping[str, str]] = None) -> bool:
    return bool(agent) and bool(token) and hmac.compare_digest(token_for(agent, env), token)


def sender_is(agent: str, sender: str) -> bool:
    """The Matrix account Logos made for the agent is @<name>-agent (matrix_host)."""
    return bool(agent) and str(sender or "").startswith(f"@{agent}-agent:")


_MARKS = re.compile(r"(\*\*|__|`{1,3}|^#{1,6}\s+|^>\s?|^\s*[-*]\s+)", re.M)


def shorten(text: str) -> str:
    """The first words as a phone shows them: no Markdown marks, one line, cut at a word."""
    t = " ".join(_MARKS.sub("", str(text or "")).split())
    if len(t) <= SHOWN:
        return t
    cut = t[:SHOWN].rsplit(" ", 1)[0] or t[:SHOWN]
    return cut.rstrip(",;:.") + "…"


class Kept:
    """event_id -> (agent, words, when), for a couple of minutes."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.items: Dict[str, Tuple[str, str, float]] = {}
        self.waiting: Dict[str, asyncio.Event] = {}

    def _tidy(self) -> None:
        now = self.clock()
        for k in [k for k, (_, _, t) in self.items.items() if now - t > KEEP_S]:
            del self.items[k]
        while len(self.items) > MAX_KEPT:
            del self.items[next(iter(self.items))]

    def put(self, event_id: str, agent: str, words: str) -> None:
        self._tidy()
        self.items[event_id] = (agent, words, self.clock())
        ev = self.waiting.pop(event_id, None)
        if ev:
            ev.set()

    def get(self, event_id: str) -> Optional[Tuple[str, str]]:
        self._tidy()
        got = self.items.get(event_id)
        return (got[0], got[1]) if got else None

    async def wait(self, event_id: str, timeout: float = WAIT_S) -> Optional[Tuple[str, str]]:
        got = self.get(event_id)
        if got or timeout <= 0:
            return got
        ev = self.waiting.setdefault(event_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            if self.waiting.get(event_id) is ev and not ev.is_set():
                self.waiting.pop(event_id, None)
        return self.get(event_id)


def words_for(kept: "Kept", event_id: str, sender: str) -> Optional[str]:
    got = kept.get(event_id)
    return got[1] if got and sender_is(got[0], sender) else None


async def wait_words(kept: "Kept", event_id: str, sender: str, timeout: float = WAIT_S) -> Optional[str]:
    """The words for an alert, when the message is from an agent that said what it was."""
    if not event_id or "-agent:" not in str(sender or ""):
        return None                                   # a person's message: never waits
    got = await kept.wait(event_id, timeout)
    return got[1] if got and sender_is(got[0], sender) else None


async def handle_put(request: web.Request) -> web.Response:
    """POST /api/v2/alert-text {agent, token, event_id, text} — from an agent's plugin."""
    try:
        body = await request.json()
        agent, token = str(body["agent"]), str(body["token"])
        event_id, text = str(body["event_id"]), str(body.get("text") or "")
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", agent) or not valid(agent, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not event_id.startswith("$") or len(event_id) > 255:
        return web.json_response({"error": "bad_request"}, status=400)
    words = shorten(text[:4000])
    if words:
        request.app["alert_text"].put(event_id, agent, words)
    return web.json_response({"kept": bool(words)})


def register(app: web.Application) -> None:
    app["alert_text"] = Kept()
    app.router.add_post(PATH, handle_put)
