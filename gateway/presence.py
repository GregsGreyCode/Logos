"""Private things are said out loud only when their person's phone is home (Voice decision D2, Greg 27 Nov).

The house speaker is in a shared room. Greg's decision: an agent may read out someone's private
details (their own email, personal calendar, messages, private notes) only while that person's
phone is on the home network. Otherwise the speaker says the details are in the chat, and they are
written there (the speaker's exchange is copied into the agent's chat: logos-speaker-chat).

- **Whose phone:** ``~/.config/logos/presence`` (not in git) maps a dashboard username to a Home
  Assistant tracker, one per line::

      greg=device_tracker.greg_s_phone
      bere=device_tracker.bere_s_phone

- **Home** means the tracker's state is ``home`` (the Companion app's location or Wi-Fi report).
  Anything else, an unknown person, no mapping, or Home Assistant not answering counts as away:
  the cautious answer.
- **Shared household things** (the lists, the lights, the household calendar) are not private.
- Checked through Logos's own non-admin Home Assistant account (``~/.config/logos/home-assistant``,
  the house service's), at most every 30 seconds per person, and never logged beyond home/away.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

PRESENCE_FILE = "presence"
CACHE_S = 30.0
TIMEOUT_S = 2.0
MARK = "Details:"

RULE = ("Do not say private details aloud: anyone's own email, personal calendar entries, messages or "
        "private notes (shared household things such as the lists, the lights and the household calendar "
        "are fine). If the answer needs private details, say in one sentence that they are in the chat, "
        f"then write them after a line that says exactly \"{MARK}\". Only what comes before that line is "
        "read out; the whole answer goes to the chat.")

_cache: Dict[str, Tuple[float, Optional[bool]]] = {}


def _config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def trackers(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """username -> tracker entity, from ~/.config/logos/presence."""
    path = _config_dir(env if env is not None else os.environ) / PRESENCE_FILE
    try:
        text = path.read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            user, entity = (p.strip() for p in line.split("=", 1))
            if user and re.fullmatch(r"(device_tracker|person)\.[a-z0-9_]+", entity):
                out[user] = entity
    return out


def _state(entity: str, env: Mapping[str, str]) -> Optional[str]:
    from gateway import house_host
    ha = house_host.ha_settings(env)
    req = urllib.request.Request(f"{ha['url']}/api/states/{entity}",
                                 headers={"Authorization": f"Bearer {ha['token']}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:   # noqa: S310 - fixed LAN address
        return str(json.loads(resp.read()).get("state") or "")


async def at_home(username: Optional[str], env: Optional[Mapping[str, str]] = None,
                  clock=time.monotonic) -> bool:
    """Whether this person's phone is home. False when it cannot be told."""
    if not username:
        return False
    env = env if env is not None else os.environ
    entity = trackers(env).get(username)
    if not entity:
        return False
    hit = _cache.get(username)
    if hit and clock() - hit[0] < CACHE_S:
        return bool(hit[1])
    try:
        home = (await asyncio.to_thread(_state, entity, env)) == "home"
    except Exception as e:  # Home Assistant down, no token: away, to be safe
        logger.info("presence: %s unknown (%s)", username, type(e).__name__)
        home = False
    _cache[username] = (clock(), home)
    logger.info("presence: %s %s", username, "home" if home else "away")
    return home


def rule(private_ok: bool) -> str:
    """The extra spoken-answer instruction: none when private details may be said."""
    return "" if private_ok else RULE


def spoken_part(words: str) -> str:
    """What is read out: everything before the ``Details:`` line (the rest is for the chat)."""
    m = re.search(rf"(?m)^\s*\**{re.escape(MARK)}", str(words or ""))
    return words[:m.start()].rstrip() if m else words
