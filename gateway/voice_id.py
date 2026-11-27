"""Who is speaking at the house speaker (Voice VO.2), and Settings › Your voice.

The speaker's audio reaches Logos through its own speech-to-text (gateway/stt.py, the
``logos-stt`` container), which keeps an embedding of each command's voice for two minutes under
a hash of its words. When those words arrive at ``POST /api/v2/voice/converse``, :func:`identify`
asks for that embedding and compares it with the voice prints here: ``known`` (that person),
``not_sure`` or ``someone_else``. Then (gateway/voice_converse.py):

- **The agent is told who it is** (:func:`instructions`), so Home answers for that person ("what's
  on today?" is their day, "add milk" is from them), and the words carry a ``[Speaker: Name]`` tag
  for the chat post (infra/agent-plugins/logos-speaker-chat).
- **A personal agent answers only its own people** (:func:`refusal`): anyone else, or a voice it is
  not sure of, gets a short polite no, and the agent is not run. Home answers anyone.

A voice print is a strong hint, not a lock: a recording of someone can fool it.

Prints are kept in the dashboard's database (``voice_prints``): embeddings and their strictness,
never audio. Each person sees and manages only their own:

    GET    /api/v2/voiceprint                    {on, enrolled, summary}
    POST   /api/v2/voiceprint?step=enrol|me|other    a recording (raw audio body)
    DELETE /api/v2/voiceprint                    forget my voice
    GET    /api/v2/voiceprint/heard              what the speaker heard in the last two minutes, scored against me
    POST   /api/v2/voiceprint/heard/<id>         "that was me": learn the speaker's microphone

Logged: the state, the person and the score. Never the words.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from gateway import stt_host
from gateway import voiceprint as vp

logger = logging.getLogger(__name__)

TIMEOUT_S = 2.0               # never hold the speaker up: without an answer, "not sure"
RECENT_S = 20.0               # no match by words: the one command heard this recently, if only one
MAX_RECORDING = 4 * 1024 * 1024
CLAIM_S = 120

PROMPT = ("Read this aloud, at your usual pace: “What's on my calendar today, and is anything on tomorrow? "
          "Add milk, eggs and some bread to the shopping list. Remind me to call the dentist on Thursday "
          "morning. Turn off the lights in the living room and the kitchen.” Then count slowly from twenty "
          "to thirty.")


@dataclass
class Who:
    state: str                    # vp.ME ("known"), vp.UNSURE ("not_sure"), vp.OTHER ("someone_else")
    username: Optional[str] = None
    name: str = ""
    score: float = 0.0
    enrolled: tuple = ()          # who has a voice print at all

    @property
    def known(self) -> bool:
        return self.state == vp.ME and bool(self.username)


# ── Prints ───────────────────────────────────────────────────────────────────────────────────

def load_prints() -> Dict[str, vp.Profile]:
    import gateway.auth.db as auth_db
    getter = getattr(auth_db, "get_voice_prints", None)
    out = {}
    try:
        rows = getter() if getter else {}
    except Exception:                 # no database yet
        return {}
    for user, data in rows.items():
        try:
            out[user] = vp.Profile.from_json(data)
        except (ValueError, TypeError):
            continue
    return out


def save_print(username: str, profile: Optional[vp.Profile]) -> None:
    import gateway.auth.db as auth_db
    auth_db.set_voice_print(username, profile.to_json() if profile is not None else None)


def display_name(username: Optional[str]) -> str:
    if not username:
        return ""
    try:
        import gateway.auth.db as auth_db
        row = auth_db.get_user_by_username(username) or {}
    except Exception:
        row = {}
    return str(row.get("display_name") or username.capitalize())


# ── The speech-to-text container's HTTP door ─────────────────────────────────────────────────

async def _stt(method: str, path: str, *, data: Optional[bytes] = None, timeout: float = TIMEOUT_S) -> Any:
    import aiohttp
    key = await asyncio.to_thread(stt_host.token)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request(method, stt_host.base_url() + path, data=data,
                             headers={"Authorization": f"Bearer {key}"}) as r:
            if r.status == 422:
                raise ValueError("unreadable")
            r.raise_for_status()
            return await r.json()


_cohort: List[list] = []


async def cohort() -> List[list]:
    if not _cohort:
        got = (await _stt("GET", "/cohort", timeout=60.0)).get("cohort") or []
        _cohort[:] = got
    return _cohort


def pick(rows: List[dict], now: Optional[float] = None) -> Optional[dict]:
    """The row heard for these words (newest first); else None."""
    return rows[0] if rows else None


def recent_only(rows: List[dict], now: Optional[float] = None) -> Optional[dict]:
    """No match by words (Home Assistant changed them): the one command heard in the last few
    seconds, when there is exactly one."""
    now = time.time() if now is None else now
    fresh = [r for r in rows if now - float(r.get("at") or 0) <= RECENT_S]
    return fresh[0] if len(fresh) == 1 else None


# ── Who is it ────────────────────────────────────────────────────────────────────────────────

def decide(prints: Mapping[str, vp.Profile], row: Optional[dict]) -> Who:
    if row is None or not row.get("embedding"):
        state, user, score = vp.UNSURE, None, 0.0
    else:
        state, user, score = vp.identify(prints, row["embedding"], float(row.get("speech") or 0))
    return Who(state, user if state == vp.ME else None, display_name(user) if state == vp.ME else "", score)


async def identify(app: Any, text: str) -> Optional[Who]:
    """Who said ``text`` at the speaker; None when nobody has a voice print yet (nothing to tell)."""
    prints = await asyncio.to_thread(load_prints)
    if not prints:
        return None
    row = None
    try:
        row = pick((await _stt("GET", f"/heard?key={vp.heard_key(text)}")).get("heard") or [])
        if row is None:
            row = recent_only((await _stt("GET", "/heard")).get("heard") or [])
    except Exception as e:                      # Home Assistant's own speech-to-text, or the container is down
        logger.info("voice id: nothing heard to compare (%s)", type(e).__name__)
    who = decide(prints, row)
    who.enrolled = tuple(sorted(prints))
    if who.known and row is not None:
        p = prints[who.username]
        if p.learn(row["embedding"], who.score, row):
            await asyncio.to_thread(save_print, who.username, p)
    logger.info("voice id: %s%s (score %.2f)", who.state, f" {who.username}" if who.username else "", who.score)
    return who


_UNKNOWN = re.compile(r"Whoever is speaking has not been identified[^.]*\.")


def says(who: Optional[Who]) -> str:
    if who is None:
        return ""
    if who.known:
        n = who.name
        return (f"The person speaking is {n} (recognised by their voice). Answer for {n}: \"I\", \"me\" and "
                f"\"my\" mean {n} (their day, their calendar), and anything they add or ask for is from {n}.")
    if who.state == vp.OTHER:
        return ("The voice was not recognised as anyone who lives here: treat them as a guest, and do not "
                "share anyone's private details.")
    return ("Who is speaking could not be told for sure: it may be Greg, Béré or a guest. If the answer "
            "depends on who is asking, ask their name.")


def instructions(base: str, who: Optional[Who]) -> str:
    """The spoken-answer instructions with who is speaking in place of "not identified"."""
    line = says(who)
    if not line:
        return base
    if _UNKNOWN.search(base):
        return _UNKNOWN.sub(lambda _: line, base, count=1)
    return f"{base} {line}"


def tagged(text: str, who: Optional[Who]) -> str:
    """The words as the agent receives them: with who said them, when known (the chat post shows it)."""
    return f"[Speaker: {who.name}] {text}" if who is not None and who.known else text


def _owners(definition: Mapping[str, Any]) -> Optional[List[str]]:
    from gateway import agent_chat
    return agent_chat.owners_of(definition or {})


def refusal(definition: Mapping[str, Any], who: Optional[Who]) -> Optional[str]:
    """What a personal agent says instead of answering someone who is not its person; None to answer.
    The Household agent (and any agent for everyone) answers anyone."""
    owners = _owners(definition)
    if owners is None or (definition or {}).get("role") == "household":
        return None
    if who is not None and who.known and who.username in owners:
        return None
    names = " or ".join(display_name(o) for o in owners) or "my person"
    if who is None or not set(owners) & set(who.enrolled):
        return (f"Sorry, I only answer {names}, and I don't know their voice yet. They can teach me in "
                "Logos, Settings, Your voice.")
    if who.state == vp.UNSURE:
        return f"Sorry, I couldn't tell who that was, and I only answer {names}. Could you say it again?"
    return f"Sorry, I only answer {names}. Home can help you."


# ── Settings › Your voice ────────────────────────────────────────────────────────────────────

def _json_error(status: int, error: str, message: str) -> Any:
    from aiohttp import web
    return web.json_response({"error": error, "message": message}, status=status)


OFF = "Speech to text for the house speaker is not switched on on this Logos yet."


def _on() -> bool:
    try:
        return stt_host.settings() is not None
    except Exception:
        return False


async def handle_get(request: Any) -> Any:
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    prints = await asyncio.to_thread(load_prints)
    mine = prints.get(user["username"])
    return web.json_response({"on": await asyncio.to_thread(_on), "enrolled": mine is not None,
                              "summary": mine.summary() if mine else None, "prompt": PROMPT,
                              "seconds": 12, "need": vp.MIN_ENROL_S})


async def _recording(request: Any) -> bytes:
    from gateway import dashboard_api as d
    audio, _ = await d._read_recording(request)
    return audio


async def handle_post(request: Any) -> Any:
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    step = request.query.get("step", "enrol")
    if step not in ("enrol", "me", "other"):
        return _json_error(400, "step", "Unknown step.")
    try:
        audio = await _recording(request)
    except d.VoiceError as e:
        return _json_error(e.status, e.error, e.message)
    if len(audio) > MAX_RECORDING:
        return _json_error(413, "too_large", "That recording is too long.")
    prints = await asyncio.to_thread(load_prints)
    mine = prints.get(user["username"])
    if step != "enrol" and mine is None:
        return _json_error(409, "not_enrolled", "Record your voice first.")
    try:
        got = await _stt("POST", "/embed", data=audio, timeout=60.0)
    except ValueError:
        return _json_error(422, "unreadable", "That recording could not be read. Try again.")
    except Exception as e:
        logger.warning("voice id: the speech-to-text service did not answer (%s)", type(e).__name__)
        return _json_error(503, "stt_unreachable", OFF if not await asyncio.to_thread(_on)
                           else "The speech-to-text service did not answer. Try again in a minute.")
    q = got.get("quality") or {}
    if step == "enrol":
        problem = vp.problem(q)
        if problem or not got.get("pieces"):
            return web.json_response({"ok": False, "quality": q, "problem": problem or "Record again."})
        try:
            others = await cohort()
        except Exception:
            others = []
        samples = list(got["pieces"]) + ([got["embedding"]] if got.get("embedding") else [])
        p = vp.enrol(samples, others, before=mine if request.query.get("add") == "1" else None)
        await asyncio.to_thread(save_print, user["username"], p)
        logger.info("voice id: %s enrolled (%d samples, strictness %.2f)", user["username"], len(p.samples), p.threshold)
        return web.json_response({"ok": True, "quality": q, "summary": p.summary()})
    problem = vp.problem(q, vp.MIN_TEST_S)
    if problem or not got.get("embedding"):
        return web.json_response({"ok": False, "quality": q, "problem": problem or "Record again, and keep talking."})
    score = mine.score(got["embedding"])
    if step == "other":
        mine.impostors.append(round(score, 4))
        mine.calibrate()
        await asyncio.to_thread(save_print, user["username"], mine)
    # Would the speaker take it for someone else who has a print? (The test is against everyone.)
    state, who, _ = vp.identify(prints | {user["username"]: mine}, got["embedding"], float(q.get("speech") or 0))
    return web.json_response({"ok": True, "quality": q, "score": round(score, 2), "threshold": round(mine.threshold, 2),
                              "recognised": score >= mine.threshold, "as": display_name(who) if state == vp.ME else "",
                              "summary": mine.summary()})


async def handle_delete(request: Any) -> Any:
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    await asyncio.to_thread(save_print, user["username"], None)
    logger.info("voice id: %s deleted their voice print", user["username"])
    return web.json_response({"ok": True})


async def handle_heard(request: Any) -> Any:
    """The speaker's last two minutes, scored against my print only (nothing about anyone else)."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    prints = await asyncio.to_thread(load_prints)
    mine = prints.get(user["username"])
    if mine is None:
        return web.json_response({"heard": []})
    try:
        rows = (await _stt("GET", "/heard")).get("heard") or []
    except Exception:
        return web.json_response({"heard": [], "problem": "The speech-to-text service did not answer."})
    now = time.time()
    out = []
    for r in rows:
        if not r.get("embedding"):
            continue
        s = mine.score(r["embedding"])
        out.append({"id": r["id"], "ago": int(now - float(r["at"])), "speech": r.get("speech"),
                    "score": round(s, 2), "recognised": s >= mine.threshold})
    return web.json_response({"heard": out, "threshold": round(mine.threshold, 2)})


async def handle_claim(request: Any) -> Any:
    """"That was me": the speaker's microphone, learned. Not when it sounds more like someone else."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    prints = await asyncio.to_thread(load_prints)
    mine = prints.get(user["username"])
    if mine is None:
        return _json_error(409, "not_enrolled", "Record your voice first.")
    try:
        rows = (await _stt("GET", "/heard")).get("heard") or []
    except Exception:
        return _json_error(503, "stt_unreachable", "The speech-to-text service did not answer.")
    row = next((r for r in rows if str(r.get("id")) == request.match_info["id"] and r.get("embedding")), None)
    if row is None or time.time() - float(row["at"]) > CLAIM_S:
        return _json_error(404, "gone", "That is no longer kept: say something to the speaker again.")
    state, who, _ = vp.identify(prints, row["embedding"], float(row.get("speech") or 0))
    if state == vp.ME and who != user["username"]:
        return _json_error(409, "someone_else", "That sounded like someone else who has a voice print, so it was not added.")
    if float(row.get("speech") or 0) < 1.0:
        return _json_error(422, "short", "That was too short to learn from.")
    mine.add_learned(row["embedding"])
    await asyncio.to_thread(save_print, user["username"], mine)
    score = mine.score(row["embedding"])
    return web.json_response({"ok": True, "score": round(score, 2), "summary": mine.summary()})


def register(app: Any) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/voiceprint", handle_get)
    app.router.add_post("/api/v2/voiceprint", require_csrf(handle_post))
    app.router.add_delete("/api/v2/voiceprint", require_csrf(handle_delete))
    app.router.add_get("/api/v2/voiceprint/heard", handle_heard)
    app.router.add_post("/api/v2/voiceprint/heard/{id}", require_csrf(handle_claim))
