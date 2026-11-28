"""Talk to an agent through a Home Assistant voice speaker (Voice tab, stories V1.1 and V3.2).

Home Assistant's Assist pipeline turns speech into words, hands the words to its conversation
agent and reads the answer out. The ``logos`` Home Assistant integration (repo
``logos-voice-pe``, ``custom_components/logos``) is that conversation agent: it posts the
words here and speaks what comes back.

    POST /api/v2/voice/converse
    Authorization: Bearer <token from ~/.config/logos/voice-converse.token>
    {"text": "...", "language": "en", "device_id": "...", "conversation_id": "..."}
    -> {"speech": "...", "conversation_id": "...", "continue": true|false, "agent": "household"}

- **The agent called answers (VW.1).** The one whose name woke the speaker ("Hey Finn"), noted
  by the wake word service (gateway/wake_host.py); a follow-up stays with it. Otherwise the
  Household agent, unless ``LOGOS_VOICE_AGENT`` names another.
- **One conversation per speaker per day.** A follow-up ("and tomorrow?") keeps the context.
- **Spoken answers.** The agent is told the words will be read aloud: a few short sentences,
  no Markdown. Whatever Markdown slips through is taken out before speaking.
- **In French or English (V3.4).** Whichever the person spoke (gateway/speech_lang.py; Home
  Assistant's pipeline says English whatever is said): the agent is asked to answer in French when
  the words were French, and a French answer is read by the agent's French voice.
- **Follow-ups (V3.2).** When the answer ends in a question, ``continue`` is true and the speaker
  listens again without the wake word.
- **It never leaves the speaker waiting long.** After ``WAIT_S`` the person hears that the answer
  will be in the chat; the run carries on and its answer is kept in the agent's session.
- **One token for the speaker**, made on first start in ``~/.config/logos/voice-converse.token``
  (0600). Home Assistant holds a copy. Nothing else can use this path.
- **The words are never logged**: only the agent, the device, how long it took and the outcome.
- **How fast it answers is measured** (``/metrics``, Greg 27 Nov: to watch it improve with a better
  GPU). Every spoken turn goes into ``logos_voice_turn_seconds`` (agent, model, first turn of the day
  or a follow-up, outcome). A fixed check asks the same two questions every night at
  ``LOGOS_VOICE_PROBE_AT`` (default 04:30; ``off`` turns it off), one in a fresh conversation and one
  follow-up, so the numbers stay comparable whatever people asked that day:
  ``logos_voice_probe_seconds``.
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

from gateway import impostor, presence, speaker_voice, speech_lang, voice_id, wake_host

logger = logging.getLogger(__name__)

PATH = "/api/v2/voice/converse"
TOKEN_FILE = "voice-converse.token"
DEFAULT_AGENT = "household"
WAIT_S = 45.0          # Home Assistant waits; a person standing at a speaker should not wait longer
MAX_TEXT = 2000        # one spoken request; anything longer is not speech
MAX_SPOKEN = 700       # about 45 seconds read aloud

BUCKETS = (2, 4, 6, 8, 10, 15, 20, 30, 45, 60)
PROBE = ("What day is it today?", "And what time is it?")   # never change: the point is comparability
PROBE_AT_ENV = "LOGOS_VOICE_PROBE_AT"
PROBE_AT = "04:30"

SLOW = "That's taking me a while. I'll put the answer in our chat."
OFF = "My model is switched off right now. Try again in a few minutes."
DOWN = "I can't reach my agent right now. Try again in a minute."
FAILED = "Sorry, something went wrong while I was working that out."
#: The same, for someone who spoke French.
FRENCH = {
    SLOW: "Ça me prend un moment. Je mettrai la réponse dans notre conversation.",
    OFF: "Mon modèle est éteint pour le moment. Réessaie dans quelques minutes.",
    DOWN: "Je n'arrive pas à joindre mon agent. Réessaie dans une minute.",
    FAILED: "Désolé, quelque chose s'est mal passé.",
}

INSTRUCTIONS = (
    "This message was spoken aloud to the house speaker and your reply will be read out by a "
    "text-to-speech voice. Whoever is speaking has not been identified: it may be Greg, Béré "
    "or a guest. Reply in plain spoken words, in the language they spoke (English or French), in one "
    "to three short sentences. Use no "
    "Markdown, lists, tables, emoji, links or code. Say numbers and times the way a person "
    "would. If the request needs long work or a long answer, do it, say in one sentence that "
    "the details are in the chat, and keep the spoken part short. If you need something from "
    "the person to go on, end with one short question. Answer promptly; don't deliberate at length."
)
IN_FRENCH = "They spoke French: reply in French."
#: A language switched off for this agent in Settings › Speaker: the answer comes in its main language.
ANSWER_IN = "They spoke {heard}, which you do not speak at the speaker: reply in {lang}."


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


def _people() -> dict:
    """username -> display name of everyone who can sign in (whom a speaker could claim to be)."""
    try:
        import gateway.auth.db as auth_db
        return {u["username"]: str(u.get("display_name") or u["username"].capitalize())
                for u in auth_db.list_users(limit=100)[0] if u.get("username")}
    except Exception:
        return {}


async def check_claim(app: Any, session_id: str, text: str, who: Any, agent: str,
                      definition: Mapping[str, Any]) -> tuple:
    """(username the speaker falsely claims to be, or None; the extra instruction for the agent)."""
    people = await asyncio.to_thread(_people)
    claim = impostor.claimed(text, people)
    suspect = claim or impostor.suspect_for(app, session_id)
    if not suspect:
        return None, ""
    prints = await asyncio.to_thread(voice_id.load_prints)
    threshold = prints[suspect].threshold if suspect in prints else None
    if impostor.verified(who, suspect, threshold):
        return None, ""                                    # it clearly is them
    if impostor.mark(app, session_id, suspect) and claim:
        shown = str((definition or {}).get("display_name") or agent)
        asyncio.ensure_future(impostor.alert(app, suspect, agent, shown))
    name = people.get(suspect) or voice_id.display_name(suspect)
    return suspect, impostor.rule(name, suspect in prints)


def without_wake(text: str, agent: str, definition: Mapping[str, Any]) -> str:
    """The words without the name that woke the speaker ("Hey Finn, what's on?" -> "what's on?").
    Home Assistant keeps the audio from just before the wake word, so the name is often heard too."""
    try:
        phrases = sorted((p["phrase"] for p in wake_host.phrases_of(agent, definition or {})), key=len, reverse=True)
    except Exception:
        return text
    for phrase in phrases:
        words = r"\W+".join(re.escape(w) for w in phrase.split())
        cut = re.sub(rf"^\W*{words}\b[\s,.!?:;-]*", "", text, count=1, flags=re.I)
        if cut != text:
            return cut if cut.strip() else text
    return text


def expects_reply(text: str) -> bool:
    """Keep listening when the answer ends by asking something."""
    return text.rstrip().rstrip('"\'”’)').endswith("?")


class Stats:
    """Turn times in memory, as a Prometheus histogram (reset on restart, which Prometheus expects)."""

    def __init__(self) -> None:
        self.turns: dict = {}   # (agent, model, turn, outcome) -> [bucket counts..., sum, count]
        self.probe: dict = {}   # (agent, model, turn) -> (seconds, outcome, timestamp)

    def observe(self, agent: str, model: str, turn: str, outcome: str, seconds: float) -> None:
        row = self.turns.setdefault((agent, model, turn, outcome), [0] * len(BUCKETS) + [0.0, 0])
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                row[i] += 1
        row[-2] += seconds
        row[-1] += 1

    def render(self) -> str:
        from gateway.metrics import esc
        out: list[str] = []
        if self.turns:
            out += ["# HELP logos_voice_turn_seconds How long a question through the house speaker took to answer.",
                    "# TYPE logos_voice_turn_seconds histogram"]
            for (agent, model, turn, outcome), row in sorted(self.turns.items()):
                lab = f'agent="{esc(agent)}",model="{esc(model)}",turn="{turn}",outcome="{outcome}"'
                for i, b in enumerate(BUCKETS):
                    out.append(f'logos_voice_turn_seconds_bucket{{{lab},le="{b}"}} {row[i]}')
                out.append(f'logos_voice_turn_seconds_bucket{{{lab},le="+Inf"}} {row[-1]}')
                out.append(f"logos_voice_turn_seconds_sum{{{lab}}} {row[-2]:.3f}")
                out.append(f"logos_voice_turn_seconds_count{{{lab}}} {row[-1]}")
        if self.probe:
            out += ["# HELP logos_voice_probe_seconds The nightly fixed check: the same questions, fresh (first) and follow-up.",
                    "# TYPE logos_voice_probe_seconds gauge"]
            for (agent, model, turn), (sec, outcome, _) in sorted(self.probe.items()):
                out.append(f'logos_voice_probe_seconds{{agent="{esc(agent)}",model="{esc(model)}",turn="{turn}",outcome="{outcome}"}} {sec:.3f}')
            out += ["# HELP logos_voice_probe_timestamp_seconds When the nightly check last ran.",
                    "# TYPE logos_voice_probe_timestamp_seconds gauge"]
            for (agent, model, turn), (_, _, at) in sorted(self.probe.items()):
                out.append(f'logos_voice_probe_timestamp_seconds{{agent="{esc(agent)}",model="{esc(model)}",turn="{turn}"}} {int(at)}')
        return "\n".join(out) + ("\n" if out else "")


def model_of(definition: Mapping[str, Any]) -> str:
    return str(((definition or {}).get("model") or {}).get("default") or "unknown")


async def is_first(client: Any, session_id: str) -> bool:
    """No such session yet: this turn pays for reading the agent's whole instructions."""
    from gateway.hermes_client import HermesNotFoundError
    try:
        await client.get_session(session_id)
        return False
    except HermesNotFoundError:
        return True
    except Exception:
        return False


async def collect(client: Any, text: str, *, session_id: str, wait: float = WAIT_S,
                  instructions: str = INSTRUCTIONS) -> tuple[str, str]:
    """Run one turn and return (outcome, words). Outcome: ok, slow, failed, down."""
    from gateway.hermes_client import HermesError

    try:
        run_id = await client.start_run(text, session_id=session_id, instructions=instructions)
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

    # The agent whose name woke the speaker (VW.1), else the one this conversation started with.
    agent = wake_host.agent_for(str(body.get("conversation_id") or "")) \
        or os.environ.get("LOGOS_VOICE_AGENT") or DEFAULT_AGENT
    session_id = session_for(agent, device)
    started = time.monotonic()

    stats: Stats = request.app["voice_stats"]
    measured: dict = {}

    heard = speech_lang.detect(text)           # V3.4: Home Assistant's "language" is the pipeline's
    lang = speaker_voice.answer_language(agent, heard)       # a language switched off: the agent's main one

    def answer(outcome: str, words: str) -> "web.Response":
        said = spoken(presence.spoken_part(words)) or FAILED
        if lang == "fr":
            said = FRENCH.get(said, said)
        # Read out in this agent's voice (V1.2), marked for Home Assistant's phrase cache (V3.3).
        speech = speaker_voice.said(request.app, agent, said)
        if measured:
            stats.observe(agent, measured["model"], measured["turn"], outcome, time.monotonic() - started)
        logger.info("voice: %s via %s: %s in %.1fs (%s)", agent, device[:12], outcome, time.monotonic() - started, lang)
        return web.json_response({"speech": speech, "conversation_id": session_id, "agent": agent,
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

    # VO.2: who is speaking, from their voice (gateway/voice_id.py). A personal agent answers only its people.
    who = await voice_id.identify(request.app, text)
    # Words that claim to be someone whose voice this clearly is not (gateway/impostor.py, Greg 27 Nov).
    suspect, extra = await check_claim(request.app, session_id, text, who, agent, definition)
    if suspect:
        if who is not None and who.username == suspect:      # a borderline match does not count now
            who = voice_id.Who(voice_id.vp.UNSURE, enrolled=who.enrolled)
        owners = voice_id._owners(definition)
        if owners is not None and definition.get("role") != "household" and suspect in owners:
            return answer("refused", impostor.refusal(voice_id.display_name(suspect)))
    refused = voice_id.refusal(definition, who)
    if refused:
        return answer("refused", refused)

    # A local model that was unloaded is loaded first, as for the web chat.
    try:
        async for payload in agent_chat.wake_model(
                definition.get("model") or {}, links.runtime.runner.http,
                backup=await asyncio.to_thread(agent_chat.has_backup, definition)):
            if payload.get("error_type") == agent_chat.MODEL_OFF:
                return answer("off", OFF)
    except Exception as e:
        logger.warning("voice: waking the model failed: %s", e)

    measured.update(model=model_of(definition),
                    turn="first" if await is_first(client, session_id) else "followup")
    # D2: private details out loud only while the speaker's own phone is home (gateway/presence.py).
    private_ok = await presence.at_home(who.username if who is not None and who.known else None)
    instructions = " ".join(p for p in (voice_id.instructions(INSTRUCTIONS, who), presence.rule(private_ok), extra,
                                        language_rule(heard, lang)) if p)
    outcome, words = await collect(client, voice_id.tagged(without_wake(text, agent, definition), who),
                                   session_id=session_id, instructions=instructions)
    return answer(outcome, words)


def language_rule(heard: str, lang: str) -> str:
    """What to tell the agent about the language of its answer."""
    if heard != lang:
        return ANSWER_IN.format(heard=speech_lang.name(heard), lang=speech_lang.name(lang))
    return IN_FRENCH if lang == "fr" else ""


def probe_time(env: Optional[Mapping[str, str]] = None) -> Optional[tuple[int, int]]:
    raw = (env if env is not None else os.environ).get(PROBE_AT_ENV, PROBE_AT).strip().lower()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return int(m[1]), int(m[2])


def seconds_until(at: tuple[int, int], now: datetime) -> float:
    target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_probe(app: "Any", agent: Optional[str] = None) -> None:
    """Ask the fixed questions in a fresh conversation and record both times."""
    agent = agent or os.environ.get("LOGOS_VOICE_AGENT") or DEFAULT_AGENT
    links = app.get("agent_links")
    if links is None:
        return
    try:
        definition = await asyncio.to_thread(links.runtime.load, agent)
        await links.health(agent)
        client = await links.client(agent)
    except Exception as e:
        logger.info("voice probe: %s not reachable, skipped: %s", agent, e)
        return
    from gateway import agent_chat
    try:
        async for payload in agent_chat.wake_model(definition.get("model") or {}, links.runtime.runner.http,
                                                   backup=await asyncio.to_thread(agent_chat.has_backup, definition)):
            if payload.get("error_type") == agent_chat.MODEL_OFF:
                logger.info("voice probe: the model server is off, skipped")
                return
    except Exception:
        pass
    model = model_of(definition)
    session_id = f"voice-probe-{_SAFE.sub('-', agent)}-{datetime.now():%Y%m%d-%H%M%S}"
    for turn, question in zip(("first", "followup"), PROBE):
        t0 = time.monotonic()
        outcome, _ = await collect(client, question, session_id=session_id, wait=180.0)
        sec = time.monotonic() - t0
        app["voice_stats"].probe[(agent, model, turn)] = (sec, outcome, time.time())
        logger.info("voice probe: %s %s %s in %.1fs", agent, turn, outcome, sec)
        if outcome != "ok":
            return


async def _probe_loop(app: "Any") -> None:
    at = probe_time()
    if at is None:
        return
    while True:
        await asyncio.sleep(seconds_until(at, datetime.now()))
        try:
            await run_probe(app)
        except Exception as e:  # never take the dashboard down
            logger.warning("voice probe failed: %s", e)
        await asyncio.sleep(61)


async def _start_probe(app: "Any") -> None:
    app["voice_probe_task"] = asyncio.ensure_future(_probe_loop(app))


async def _stop_probe(app: "Any") -> None:
    task = app.get("voice_probe_task")
    if task:
        task.cancel()


def register(app: "Any") -> None:
    token()  # made now, so it can be copied into Home Assistant before the first request
    app["voice_stats"] = Stats()
    app.router.add_post(PATH, handle_converse)
    app.on_startup.append(_start_probe)
    app.on_cleanup.append(_stop_probe)
