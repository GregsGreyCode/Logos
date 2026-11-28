"""Agents speak up on the house speaker without being asked (Voice V3.1): a reminder, the morning briefing.

An agent calls its ``say_on_speaker`` tool (agent plugin ``logos-speaker-say``); the plugin posts the
words here, on the one dashboard path its sandbox may reach (``speaker_say`` in its network policy),
signed with the agent's own token. Logos decides whether they are said, and if so asks Home Assistant
to announce them on the speaker (``assist_satellite.announce``). Home Assistant reads them through
the pipeline's text-to-speech, which is Logos's own (gateway/tts.py), so the words are marked with the
agent's voice first (gateway/speaker_voice.py) and it is heard in its own voice, French or English.

    POST /api/v2/speaker/say  {agent, token, text, private, person, chime}
    -> {"spoken": true}  or  {"spoken": false, "reason": "...", "tell": "<what the agent should do instead>"}

Nothing is said aloud, and the agent is told to put it in the chat instead, when:

- **quiet hours** (``QUIET_HOURS``, default 22:00-08:00 in the agent's time zone);
- **it is private** (the agent says so: someone's own calendar, email, messages, notes) and that
  person's phone is not home (the D2 rule, gateway/presence.py). A private reminder from a person's
  own agent is for that person; the household agent has to say whose it is;
- **it is long**: at most ``MAX_CHARS`` characters, one or two short sentences;
- **too many**: ``PER_HOUR`` an hour and ``PER_DAY`` a day per agent, and never two within
  ``GAP_S`` seconds on the speaker;
- the agent is not one that speaks on the speaker (``speaker_voice`` or ``speaker_name`` in its
  definition; ``speaker_say: false`` turns it off), or no speaker is set for announcements.

The speaker's entity is set on the Logos machine, in ``~/.config/logos/speaker`` (not in git)::

    ANNOUNCE_ENTITY=assist_satellite.home_assistant_voice_xxxxxx_assist_satellite
    QUIET_HOURS=22:00-08:00          # optional; "off" for none

Home Assistant is reached with Logos's own non-admin account (``~/.config/logos/home-assistant``, the
house service's and the presence check's), for this one service only. Not through the house service:
that one serves the agents' own tool calls with its allow-list, while here Logos itself, after its
checks, makes one fixed call; the agents never name a service or an entity.

The log has the agent, the outcome and the length. Never the words.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# Only the standard library at the top: the deploy tools load this file by path (agent_runtime._sibling).

logger = logging.getLogger(__name__)

PATH = "/api/v2/speaker/say"
TOKEN_ENV = "LOGOS_SPEAKER_SAY_TOKEN"
URL_ENV = "LOGOS_SPEAKER_SAY_URL"
MAX_CHARS = 240                     # about 15 seconds read aloud
PER_HOUR = 4
PER_DAY = 12
GAP_S = 60.0
QUIET = ((22, 0), (8, 0))
TIMEOUT_S = 10.0
SERVICE = ("assist_satellite", "announce")
_ENTITY = re.compile(r"assist_satellite\.[a-z0-9_]+")

QUIET_TELL = ("Not said aloud: it is quiet hours at home ({hours}). Put it in your message in the chat "
              "instead, and do not try the speaker again for it.")
PRIVATE_TELL = ("Not said aloud: it is private and {who}'s phone is not at home, so it may be heard by "
                "someone else. Put it in your message in the chat instead.")
NO_PERSON_TELL = ("Not said aloud: it is private and you did not say whose it is (person). Put it in the "
                  "chat instead.")
LONG_TELL = ("Not said: too long for the speaker ({n} characters; at most {max}). Say it in one or two "
             "short sentences, or put it in the chat.")
LIMIT_TELL = "Not said aloud: the speaker has had enough from you for now ({why}). Put it in the chat instead."
BUSY_TELL = "Not said aloud: the speaker said something less than a minute ago. Put it in the chat instead."
OFF_TELL = "Not said: the house speaker is not set up for announcements. Put it in the chat instead."
NOT_YOURS_TELL = "Not said: you do not speak on the house speaker. Put it in the chat instead."
FAILED_TELL = "Not said: Home Assistant did not take it ({why}). Put it in the chat instead; do not retry in a loop."


class SayError(Exception):
    pass


# ── tokens: like the phone alert's (gateway/alert_text.py), under the same key, for another purpose ──

def token_for(agent: str, env: Optional[Mapping[str, str]] = None, key: Optional[bytes] = None) -> str:
    """The agent's token, from the alert key (``key``, else read through gateway.alert_text)."""
    if key is None:
        from gateway import alert_text
        key = alert_text.key(env)
    return hmac.new(key, f"speaker-say:{agent}".encode(), hashlib.sha256).hexdigest()


def valid(agent: str, token: str, env: Optional[Mapping[str, str]] = None) -> bool:
    try:
        expected = token_for(agent, env)
    except RuntimeError:                 # the alert key is unusable (gateway/alert_text.key): refuse
        return False
    return bool(agent) and bool(token) and hmac.compare_digest(expected, token)


def speaks(definition: Mapping[str, Any]) -> bool:
    """An agent that answers on the house speaker may also speak up there (``speaker_say: false``: not)."""
    d = definition or {}
    if d.get("speaker_say") is False:
        return False
    return bool(d.get("speaker_voice") or str(d.get("speaker_name") or "").strip())


# ── settings ──────────────────────────────────────────────────────────────────────────────────

def _hm(text: str) -> Tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise SayError(f"not a time: {text!r}")
    return int(m[1]), int(m[2])


def settings(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """``{"entity", "quiet"}`` from ~/.config/logos/speaker. ``entity`` "" when announcements are off;
    ``quiet`` None when there are no quiet hours."""
    from gateway import tts_host
    env = os.environ if env is None else env
    try:
        values = tts_host.parse_env((tts_host.config_dir(env) / tts_host.SETTINGS_FILE).read_text())
    except OSError:
        values = {}
    entity = values.get("ANNOUNCE_ENTITY", "").strip()
    if entity and not _ENTITY.fullmatch(entity):
        logger.warning("speaker say: ANNOUNCE_ENTITY is not an assist_satellite entity; announcements off")
        entity = ""
    raw = values.get("QUIET_HOURS", "").strip().lower()
    quiet: Optional[tuple] = QUIET
    if raw in ("off", "none", "no"):
        quiet = None
    elif raw:
        try:
            start, end = raw.split("-", 1)
            quiet = (_hm(start), _hm(end))
        except (ValueError, SayError):
            logger.warning("speaker say: QUIET_HOURS should look like 22:00-08:00; using the default")
    return {"entity": entity, "quiet": quiet}


def hours_text(quiet: Optional[tuple]) -> str:
    if not quiet:
        return "none"
    (a, b), (c, d) = quiet
    return f"{a:02d}:{b:02d} to {c:02d}:{d:02d}"


def is_quiet(quiet: Optional[tuple], now: datetime) -> bool:
    if not quiet:
        return False
    start, end = quiet
    t = (now.hour, now.minute)
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def now_for(definition: Mapping[str, Any]) -> datetime:
    """Now, in the agent's time zone (its definition's ``timezone``), else this machine's."""
    tz = str((definition or {}).get("timezone") or "")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz))
        except Exception:
            pass
    return datetime.now()


# ── how often ─────────────────────────────────────────────────────────────────────────────────

class Limits:
    """In memory: when each agent last spoke up, and when the speaker last did."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.spoken: Dict[str, List[float]] = {}
        self.last = 0.0

    def check(self, agent: str) -> str:
        """"" when it may speak now, else why not."""
        now = self.clock()
        times = [t for t in self.spoken.get(agent, []) if now - t < 86400]
        self.spoken[agent] = times
        if len([t for t in times if now - t < 3600]) >= PER_HOUR:
            return f"{PER_HOUR} times in the last hour"
        if len(times) >= PER_DAY:
            return f"{PER_DAY} times today"
        if now - self.last < GAP_S:
            return "busy"
        return ""

    def note(self, agent: str) -> None:
        now = self.clock()
        self.spoken.setdefault(agent, []).append(now)
        self.last = now


# ── Home Assistant ────────────────────────────────────────────────────────────────────────────

def announce(entity: str, message: str, chime: bool, env: Optional[Mapping[str, str]] = None) -> None:
    """Ask Home Assistant to say ``message`` on the speaker. Raises SayError when it does not."""
    from gateway import house_host
    try:
        ha = house_host.ha_settings(env if env is not None else os.environ)
    except house_host.HouseError as e:
        raise SayError(str(e))
    body = json.dumps({"entity_id": entity, "message": message, "preannounce": bool(chime)}).encode()
    req = urllib.request.Request(f"{ha['url']}/api/services/{SERVICE[0]}/{SERVICE[1]}", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {ha['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:          # noqa: S310 - fixed LAN address
            if r.status >= 300:
                raise SayError(f"answer {r.status}")
    except urllib.error.HTTPError as e:
        raise SayError(f"answer {e.code}")
    except OSError as e:
        raise SayError(type(e).__name__)


# ── the decision ──────────────────────────────────────────────────────────────────────────────

def owner_of(definition: Mapping[str, Any]) -> Optional[str]:
    """A personal agent's one person, else None (the household agent has two)."""
    from gateway import voice_id
    owners = voice_id._owners(definition)
    return owners[0] if owners and len(owners) == 1 and (definition or {}).get("role") != "household" else None


async def decide(app: Any, agent: str, definition: Mapping[str, Any], text: str, private: bool,
                 person: Optional[str], chime: bool = True, env: Optional[Mapping[str, str]] = None,
                 now: Optional[datetime] = None) -> dict:
    """Say it, or say why not. Never raises."""
    from gateway import presence, speaker_voice, voice_converse

    def no(reason: str, tell: str) -> dict:
        logger.info("speaker say: %s: %s (%d characters)", agent, reason, len(text))
        return {"spoken": False, "reason": reason, "tell": tell}

    if not speaks(definition):
        return no("not_on_speaker", NOT_YOURS_TELL)
    conf = settings(env)
    if not conf["entity"]:
        return no("off", OFF_TELL)
    words = voice_converse.spoken(text)
    if not words:
        return no("empty", "Not said: there were no words to say.")
    if len(words) > MAX_CHARS:
        return no("too_long", LONG_TELL.format(n=len(words), max=MAX_CHARS))
    if is_quiet(conf["quiet"], now or now_for(definition)):
        return no("quiet_hours", QUIET_TELL.format(hours=hours_text(conf["quiet"])))
    if private:
        who = (person or "").strip().lower() or owner_of(definition)
        if not who:
            return no("private_no_person", NO_PERSON_TELL)
        if not await presence.at_home(who, env):
            from gateway import voice_id
            return no("private_away", PRIVATE_TELL.format(who=voice_id.display_name(who)))
    limits: Limits = app["speaker_say_limits"]
    why = limits.check(agent)
    if why == "busy":
        return no("busy", BUSY_TELL)
    if why:
        return no("rate_limited", LIMIT_TELL.format(why=why))
    message = speaker_voice.said(app, agent, words, env)     # in its own voice, marked for the phrase cache
    try:
        await asyncio.to_thread(app.get("speaker_say_announce") or announce, conf["entity"], message, chime, env)
    except SayError as e:
        return no("failed", FAILED_TELL.format(why=e))
    limits.note(agent)
    logger.info("speaker say: %s: spoken (%d characters%s)", agent, len(words), ", private" if private else "")
    return {"spoken": True}


async def handle_say(request: "Any") -> "Any":
    """POST /api/v2/speaker/say {agent, token, text, private?, person?, chime?} — from an agent's plugin."""
    from aiohttp import web
    try:
        body = await request.json()
        from gateway.alert_text import request_token
        agent, token, text = str(body["agent"]), request_token(request, body), str(body.get("text") or "")[:4000]
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", agent) or not valid(agent, token):
        return web.json_response({"error": "forbidden"}, status=403)
    from gateway import speaker_voice
    definition = await asyncio.to_thread(speaker_voice._definition, request.app, agent)
    person = body.get("person")
    out = await decide(request.app, agent, definition, text, bool(body.get("private")),
                       str(person) if isinstance(person, str) else None, chime=body.get("chime") is not False)
    return web.json_response(out)


def register(app: "Any") -> None:
    app["speaker_say_limits"] = Limits()
    app.router.add_post(PATH, handle_say)
