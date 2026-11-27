"""Talk to an agent through a Home Assistant voice speaker (Voice tab, stories V1.1 and V3.2).

Home Assistant's Assist pipeline turns speech into words, hands the words to its conversation
agent and reads the answer out. The ``logos`` Home Assistant integration (repo
``logos-voice-pe``, ``custom_components/logos``) is that conversation agent: it posts the
words here and speaks what comes back.

    POST /api/v2/voice/converse
    Authorization: Bearer <token from ~/.config/logos/voice-converse.token>
    {"text": "...", "language": "en", "device_id": "...", "conversation_id": "..."}
    -> {"speech": "...", "conversation_id": "...", "continue": true|false, "agent": "household"}

- **Home answers (V1).** The Household agent, unless ``LOGOS_VOICE_AGENT`` names another.
  Routing by wake word and voice print comes in VW.2 and VO.2.
- **One conversation per speaker per day.** A follow-up ("and tomorrow?") keeps the context.
- **Spoken answers.** The agent is told the words will be read aloud: a few short sentences,
  no Markdown. Whatever Markdown slips through is taken out before speaking.
- **Follow-ups (V3.2).** When the answer ends in a question, ``continue`` is true and the speaker
  listens again without the wake word.
- **It never leaves the speaker waiting long.** After ``WAIT_S`` the person hears that the answer
  will be in the chat; the run carries on and its answer is kept in the agent's session.
- **One token for the speaker**, made on first start in ``~/.config/logos/voice-converse.token``
  (0600). Home Assistant holds a copy. Nothing else can use this path.
- **The words are never logged**: only the agent, the device, how long it took and the outcome.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

PATH = "/api/v2/voice/converse"
TOKEN_FILE = "voice-converse.token"
DEFAULT_AGENT = "household"
WAIT_S = 45.0          # Home Assistant waits; a person standing at a speaker should not wait longer
MAX_TEXT = 2000        # one spoken request; anything longer is not speech
MAX_SPOKEN = 700       # about 45 seconds read aloud

SLOW = "That's taking me a while. I'll put the answer in our chat."
OFF = "My model is switched off right now. Try again in a few minutes."
DOWN = "I can't reach my agent right now. Try again in a minute."
FAILED = "Sorry, something went wrong while I was working that out."

INSTRUCTIONS = (
    "This message was spoken aloud to the house speaker and your reply will be read out by a "
    "text-to-speech voice. Whoever is speaking has not been identified: it may be Greg, Béré "
    "or a guest. Reply in plain spoken English, in one to three short sentences. Use no "
    "Markdown, lists, tables, emoji, links or code. Say numbers and times the way a person "
    "would. If the request needs long work or a long answer, do it, say in one sentence that "
    "the details are in the chat, and keep the spoken part short. If you need something from "
    "the person to go on, end with one short question. Answer promptly; don't deliberate at length."
)


def _config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def token(env: Optional[Mapping[str, str]] = None) -> str:
    """The speaker's token: made once, owner-only, beside Logos's other keys."""
    path = _config_dir(env if env is not None else os.environ) / TOKEN_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            time.sleep(0.1)
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def authorised(header: str, env: Optional[Mapping[str, str]] = None) -> bool:
    given = header[7:].strip() if header.startswith("Bearer ") else ""
    return bool(given) and hmac.compare_digest(given.encode(), token(env).encode())


_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def session_for(agent: str, device: str, now: Optional[datetime] = None) -> str:
    """One hermes session per speaker per day: follow-ups keep their context, and a new day
    starts afresh (like the agents' own 03:00 reset)."""
    day = (now or datetime.now()).strftime("%Y%m%d")
    dev = _SAFE.sub("-", device or "speaker")[:40] or "speaker"
    return f"voice-{_SAFE.sub('-', agent)}-{dev}-{day}"


_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"(\*\*|__|\*|`|^#{1,6}\s+|^>\s?|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.M)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def spoken(text: str) -> str:
    """What the speaker should say: no Markdown, links or emoji, one paragraph, not too long."""
    t = _FENCE.sub(" ", str(text or ""))
    t = _LINK.sub(r"\1", t)
    t = _URL.sub("", t)
    t = _MARKS.sub("", t)
    t = _EMOJI.sub("", t)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    # A list read aloud: each item becomes its own sentence.
    t = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    t = " ".join(t.split())
    if len(t) > MAX_SPOKEN:
        cut = t[:MAX_SPOKEN]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[: end + 1] if end > MAX_SPOKEN // 2 else cut.rsplit(" ", 1)[0] + ".") + \
            " The rest is in the chat."
    return t


def expects_reply(text: str) -> bool:
    """Keep listening when the answer ends by asking something."""
    return text.rstrip().rstrip('"\'”’)').endswith("?")


async def collect(client: Any, text: str, *, session_id: str, wait: float = WAIT_S) -> tuple[str, str]:
    """Run one turn and return (outcome, words). Outcome: ok, slow, failed, down."""
    from gateway.hermes_client import HermesError

    try:
        run_id = await client.start_run(text, session_id=session_id, instructions=INSTRUCTIONS)
    except HermesError as e:
        logger.warning("voice: could not start a run: %s", e)
        return "down", DOWN

    async def _gather() -> tuple[str, str]:
        parts: list[str] = []
        async for ev in client.stream_events(run_id):
            if ev.type == "token":
                parts.append(ev.text)
            elif ev.type == "run_completed":
                return "ok", (ev.text or "".join(parts))
            elif ev.terminal:
                return "failed", FAILED
        return ("ok", "".join(parts)) if parts else ("failed", FAILED)

    try:
        return await asyncio.wait_for(_gather(), timeout=wait)
    except asyncio.TimeoutError:
        # Not stopped: the run finishes in the agent and its answer stays in the session.
        return "slow", SLOW
    except HermesError as e:
        logger.warning("voice: run %s broke: %s", run_id, e)
        return "failed", FAILED


async def handle_converse(request: "Any") -> "Any":
    from aiohttp import web
    from gateway import agent_chat

    if not authorised(request.headers.get("Authorization", "")):
        raise web.HTTPUnauthorized(text='{"error":"unauthorised"}', content_type="application/json")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"json"}', content_type="application/json")
    text = str(body.get("text") or "").strip()[:MAX_TEXT]
    device = str(body.get("device_id") or body.get("satellite_id") or "speaker")
    if not text:
        raise web.HTTPBadRequest(text='{"error":"text is required"}', content_type="application/json")

    agent = os.environ.get("LOGOS_VOICE_AGENT") or DEFAULT_AGENT
    session_id = session_for(agent, device)
    started = time.monotonic()

    def answer(outcome: str, words: str) -> "web.Response":
        said = spoken(words) or FAILED
        logger.info("voice: %s via %s: %s in %.1fs", agent, device[:12], outcome, time.monotonic() - started)
        return web.json_response({"speech": said, "conversation_id": session_id, "agent": agent,
                                  "continue": outcome == "ok" and expects_reply(said)})

    links = request.app.get("agent_links")
    if links is None:
        return answer("down", DOWN)
    try:
        definition = await asyncio.to_thread(links.runtime.load, agent)
        await links.health(agent)
        client = await links.client(agent)
    except Exception as e:  # unknown agent, not running, sandbox unreachable
        logger.warning("voice: %s unreachable: %s", agent, e)
        return answer("down", DOWN)

    # A local model that was unloaded is loaded first, as for the web chat.
    try:
        async for payload in agent_chat.wake_model(
                definition.get("model") or {}, links.runtime.runner.http,
                backup=await asyncio.to_thread(agent_chat.has_backup, definition)):
            if payload.get("error_type") == agent_chat.MODEL_OFF:
                return answer("off", OFF)
    except Exception as e:
        logger.warning("voice: waking the model failed: %s", e)

    outcome, words = await collect(client, text, session_id=session_id)
    return answer(outcome, words)


def register(app: "Any") -> None:
    token()  # made now, so it can be copied into Home Assistant before the first request
    app.router.add_post(PATH, handle_converse)
