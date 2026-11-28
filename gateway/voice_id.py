"""Who is speaking at the house speaker (Voice VO.2, VO.3), and Settings › Your voice.

The speaker's audio reaches Logos through its own speech-to-text (gateway/stt.py, the
``logos-stt`` container), which keeps an embedding of each command's voice for 30 minutes under
a hash of its words. When those words arrive at ``POST /api/v2/voice/converse``, :func:`identify`
asks for that embedding and compares it with the voice prints here: ``known`` (that person),
``not_sure`` or ``someone_else``. Then (gateway/voice_converse.py):

- **The agent is told who it is** (:func:`instructions`), so Home answers for that person ("what's
  on today?" is their day, "add milk" is from them), and the words carry a ``[Speaker: Name]`` tag
  for the chat post (infra/agent-plugins/logos-speaker-chat).
- **A personal agent answers only its own people** (:func:`refusal`) once their print is ACTIVE:
  anyone else, or a voice it is not sure of, gets a short polite no, and the agent is not run.
  Home answers anyone.

**LEARNING, then ACTIVE (VO.3, Greg 28 Nov: "remove recognising whose voice it is until the agent is
sure ... that it can get it right 99% of the time").** Every print starts LEARNING: nobody is named
from their voice, a personal agent answers its name without the voice check, a claim to be them is not
checked against it (and unlocks nothing private: that needs an ACTIVE, recognised person, D2). While it
listens, :func:`collect` keeps the commands its person's agent was woken for ("Hey Finn" -> Greg) as
labelled speaker samples, unless the voice is clearly unlike theirs (held back for 30 minutes, "That was
me" lets it in), scoring each against the print as it was first (the self-test). When the self-test
says it would have got it right (voiceprint.ON_*), the print switches to ACTIVE and its person is told;
if it gets worse, it falls back and they are told.

A voice print is a strong hint, not a lock: a recording of someone can fool it.

Prints are kept in the dashboard's database (``voice_prints``): embeddings, their strictness and the
self-test's record, never audio. Each person sees and manages only their own:

    GET    /api/v2/voiceprint                    {on, enrolled, summary}
    POST   /api/v2/voiceprint?step=enrol|me|other    a recording (raw audio body)
    DELETE /api/v2/voiceprint                    forget my voice
    POST   /api/v2/voiceprint/state              {"state": "learning"}: start learning again
    GET    /api/v2/voiceprint/heard              the speaker's last 30 minutes, scored against me
    POST   /api/v2/voiceprint/heard/<id>         "That was me": add it
    DELETE /api/v2/voiceprint/heard/<id>         "Not me": take out what was added from it
    GET    /api/v2/voiceprint/people             (admin) everyone's state, no voices

Logged: the state, the person and the score. Never the words.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from gateway import stt_host
from gateway import voiceprint as vp

logger = logging.getLogger(__name__)

TIMEOUT_S = 2.0               # never hold the speaker up: without an answer, "not sure"
RECENT_S = 20.0               # no match by words: the one command heard this recently, if only one
MAX_RECORDING = 4 * 1024 * 1024
CLAIM_S = 1800                # "That was me" / "Not me": the speaker's last 30 minutes (what logos-stt keeps)
MIN_SAMPLE_S = 1.0            # seconds of speech a speaker sample needs
ALERTS_FILE = "voice-alerts.json"
ALERTS_KEEP_S = 30 * 86400

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
    active: tuple = ()            # whose print is ACTIVE (recognising)
    short: bool = False           # too little speech to tell
    heard: Optional[dict] = field(default=None, repr=False)   # the speaker's row (embedding, speech, ...)

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


def active_of(prints: Mapping[str, vp.Profile]) -> tuple:
    return tuple(sorted(u for u, p in prints.items() if p.state == vp.ACTIVE))


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
_speaker_cohort: List[list] = []


async def cohort() -> List[list]:
    if not _cohort:
        got = (await _stt("GET", "/cohort", timeout=60.0)).get("cohort") or []
        _cohort[:] = got
    return _cohort


async def speaker_cohort() -> List[list]:
    """The reference voices as the speaker would hear them; [] when the container is older or down."""
    if not _speaker_cohort:
        try:
            got = (await _stt("GET", "/cohort?channel=speaker", timeout=60.0)).get("cohort") or []
        except Exception:
            return []
        _speaker_cohort[:] = got
    return _speaker_cohort


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
    speech = float((row or {}).get("speech") or 0)
    if row is None or not row.get("embedding"):
        state, user, score = vp.UNSURE, None, 0.0
    else:
        state, user, score = vp.identify(prints, row["embedding"], speech, vp.SPEAKER)
    who = Who(state, user if state == vp.ME else None, display_name(user) if state == vp.ME else "", score)
    who.short = state == vp.UNSURE and row is not None and bool(row.get("embedding")) and speech < vp.SHORT_S
    return who


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
    who.enrolled, who.active, who.heard = tuple(sorted(prints)), active_of(prints), row
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


def personal(definition: Mapping[str, Any]) -> Optional[List[str]]:
    """The owners of a personal agent (Finn); None for Home and any agent for everyone."""
    owners = _owners(definition)
    if owners is None or (definition or {}).get("role") == "household":
        return None
    return owners


def refusal(definition: Mapping[str, Any], who: Optional[Who]) -> Optional[str]:
    """What a personal agent says instead of answering someone who is not its person; None to answer.
    Home (and any agent for everyone) answers anyone; so does a personal agent while no owner's print is
    ACTIVE (it is still learning their voice: nothing private is said, D2)."""
    owners = personal(definition)
    if owners is None:
        return None
    if who is not None and who.known and who.username in owners:
        return None
    names = " or ".join(display_name(o) for o in owners) or "my person"
    if who is not None and who.known:                 # someone else of the house, recognised (ACTIVE)
        return f"Sorry {who.name}, I only answer {names}. Home can help you."
    if who is None or not set(owners) & set(who.active):
        return None                                   # still learning: answered, without private details
    if who.state == vp.UNSURE and who.short:
        return "Sorry, that was too short for me to tell who it was. Could you say it again, in a few more words?"
    if who.state == vp.UNSURE:
        return "Sorry, I couldn't tell who that was. Could you say it again?"
    return f"Sorry, I only answer {names}. Home can help you."


def learning_line(definition: Mapping[str, Any], who: Optional[Who]) -> str:
    """For a personal agent answering while its person's print is still learning."""
    owners = personal(definition)
    if not owners or who is None or who.known or set(owners) & set(who.active):
        return ""
    names = " or ".join(display_name(o) for o in owners)
    return (f"Logos is still learning {names}'s voice, so it has not confirmed who is speaking: answer as usual, "
            "but say no private details aloud.")


# ── Collecting speaker samples while learning (VO.3) ─────────────────────────────────────────

_locks: Dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    """One change to the prints at a time (per event loop: the tests run several)."""
    loop = id(asyncio.get_running_loop())
    if loop not in _locks:
        _locks.clear()
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def hkey(row: Mapping[str, Any]) -> str:
    """A heard command's lasting name: its number and when (numbers start again when logos-stt restarts)."""
    return f"{row.get('id')}:{int(float(row.get('at') or 0))}"


_held: Dict[str, List[dict]] = {}       # username -> commands held back (in memory, CLAIM_S)


def held_for(username: str, now: Optional[float] = None) -> List[dict]:
    now = time.time() if now is None else now
    rows = [h for h in _held.get(username, []) if now - h.get("kept", h["at"]) <= CLAIM_S]
    _held[username] = rows
    return rows


def _hold(username: str, row: Mapping[str, Any], why: str, score: float) -> None:
    rows = [h for h in held_for(username) if h["id"] != hkey(row)]
    rows.append({"id": hkey(row), "at": float(row.get("at") or time.time()), "kept": time.time(), "why": why,
                 "score": round(score, 3)})
    _held[username] = rows[-50:]


def _taken(prints: Mapping[str, vp.Profile], heard: Any) -> Optional[str]:
    return next((u for u, p in prints.items() if p.has_heard(heard)), None)


def false_accepts(prints: Mapping[str, vp.Profile], username: str) -> int:
    """How many of the other people's labelled speaker samples (their last ON_WINDOW) this person's print
    would take for them: the household test, in the speaker's own room and microphone."""
    mine = prints.get(username)
    if mine is None:
        return 0
    thr, n, score = mine.threshold_for(vp.SPEAKER), 0, vp.scorer(mine.samples)
    for u, p in prints.items():
        if u == username:
            continue
        theirs = [s for s, m in zip(p.speaker_samples, p.meta) if m.get("src") in vp.LABELLED][-vp.ON_WINDOW:]
        n += sum(1 for s in theirs if score(s) >= thr)
    return n


def household_samples(prints: Mapping[str, vp.Profile], username: str) -> List[List[float]]:
    return [s for u, p in prints.items() if u != username
            for s, m in zip(p.speaker_samples, p.meta) if m.get("src") in vp.LABELLED]


def recalibrate(prints: Mapping[str, vp.Profile], username: str, cohort_s: List[list]) -> None:
    if cohort_s:
        prints[username].calibrate_speaker(cohort_s, household_samples(prints, username))


def _review_all(prints: Mapping[str, vp.Profile], now: float) -> Dict[str, str]:
    changed = {}
    for u, p in prints.items():
        new = p.review(false_accepts(prints, u), now)
        if new:
            changed[u] = new
    return changed


def labelled_owner(definition: Mapping[str, Any], woken: bool) -> Optional[str]:
    """The person a command is labelled as: the one owner of the personal agent whose name woke the speaker."""
    owners = personal(definition) if woken else None
    return owners[0] if owners and len(owners) == 1 else None


def collect_sync(prints: Dict[str, vp.Profile], row: Mapping[str, Any], owner: Optional[str], claim: Optional[str],
                 cohort_s: List[list], now: Optional[float] = None) -> dict:
    """What one speaker command teaches the prints (pure: the caller loads and saves). ``owner``: the person
    it is labelled as (their agent's name woke the speaker), else None (Home). ``claim``: the person the words
    say they are. Returns {"changed": {username}, "states": {username: new state}, "why": ...}."""
    now = time.time() if now is None else now
    out: dict = {"changed": set(), "states": {}, "why": ""}
    e, speech, heard = row.get("embedding"), float(row.get("speech") or 0), hkey(row)
    if not e or speech < MIN_SAMPLE_S:
        out["why"] = "too short"
        return out
    if _taken(prints, heard):
        out["why"] = "already kept"
        return out
    if owner is None:
        # Home: only a voice that already clearly is exactly one person (and never one still learning less
        # than it needs: the self-test counts only labelled commands).
        state, u, score = vp.identify(prints, e, speech, vp.SPEAKER, everyone=True)
        if state == vp.ME and prints[u].learn(e, score, row, now=now):
            recalibrate(prints, u, cohort_s)
            out["changed"].add(u)
            out["why"] = "home: clear match"
        return out
    p = prints.get(owner)
    if p is None:
        out["why"] = "no print"
        return out
    if claim and claim != owner:
        _hold(owner, row, "claimed someone else", 0.0)
        out["why"] = "claimed someone else"
        return out
    others = {u: q for u, q in prints.items() if u != owner}
    state, u, score = vp.identify(others, e, speech, vp.SPEAKER) if others else (vp.OTHER, None, 0.0)
    if state == vp.ME:                                # clearly someone else's ACTIVE print
        _hold(owner, row, "sounded like someone else", score)
        out["why"] = "someone else"
        return out
    # The self-test: would the print as it was have recognised this command?
    st, who, s = vp.identify(prints, e, speech, vp.SPEAKER, everyone=True)
    ok = st == vp.ME and who == owner
    trial = speech >= vp.SHORT_S
    if p.state == vp.ACTIVE:
        # Recognising: only what it recognised is added. A miss counts only when the person says "That was
        # me" (a stranger saying the name again and again must not switch the check off).
        if ok:
            p.add_speaker(e, row, "name", heard=heard, now=now)
            if trial:
                p.record_trial(True, s, heard, now)
            recalibrate(prints, owner, cohort_s)
            out["changed"].add(owner)
        else:
            _hold(owner, row, "not recognised", s)
        out["why"] = "active"
    else:
        admitted, why, s2 = p.admission(e, others, cohort_s)
        if admitted:
            if trial:
                p.record_trial(ok, s, heard, now)
            p.add_speaker(e, row, "name", heard=heard, now=now)
            out["pruned"] = p.prune()
            recalibrate(prints, owner, cohort_s)
        else:
            if trial:
                p.record_trial(None, s, heard, now)       # held back: logged, never a miss
            _hold(owner, row, {"unlike": "did not sound like you", "someone_else": "sounded more like someone else",
                               "stranger": "too unlike you to be sure"}.get(why, why), s2)
        out["changed"].add(owner)
        out["why"] = why
    out["states"] = _review_all(prints, now)
    out["changed"] |= set(out["states"])
    return out


async def collect(app: Any, who: Optional[Who], definition: Mapping[str, Any], woken: bool,
                  claim: Optional[str] = None) -> None:
    """After a speaker command: learn from it (:func:`collect_sync`), save, and tell anyone whose print
    switched on or off. Never raises (the speaker has already been answered)."""
    try:
        if who is None or not who.heard:
            return
        owner = labelled_owner(definition, woken)
        cohort_s = await speaker_cohort()
        async with _lock():
            prints = await asyncio.to_thread(load_prints)
            if not prints:
                return
            got = await asyncio.to_thread(collect_sync, prints, who.heard, owner, claim, cohort_s)
            for u in got["changed"]:
                await asyncio.to_thread(save_print, u, prints[u])
        if owner:
            logger.info("voice id: speaker sample for %s: %s", owner, got["why"])
        for u, state in got["states"].items():
            await _tell(app, u, state)
    except Exception as e:                            # learning must never break the speaker
        logger.warning("voice id: collecting failed: %s", type(e).__name__)


# ── Telling the person when it switches on or off ───────────────────────────────────────────

def _alerts_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = env if env is not None else os.environ
    return Path(env.get("LOGOS_HOME") or Path(env.get("HOME", str(Path.home()))) / ".logos") / ALERTS_FILE


def _alerts(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


TOLD = {vp.ACTIVE: ("The house speaker now recognises your voice",
                    "Your own agent at the speaker now answers only you, and Home knows when it is you."),
        vp.LEARNING: ("The house speaker is learning your voice again",
                      "It was not recognising you well enough, so it stopped checking for now. Keep using it as usual.")}


def record_alert(username: str, state: str, now: Optional[float] = None, env: Optional[Mapping[str, str]] = None) -> dict:
    now = time.time() if now is None else now
    path = _alerts_path(env)
    items = [a for a in _alerts(path) if now - float(a.get("ts") or 0) < ALERTS_KEEP_S]
    item = {"id": f"voiceprint:{int(now)}:{state}", "user": username, "state": state, "ts": now}
    items = (items + [item])[-50:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items, f)
    os.replace(tmp, path)
    return item


def notifications_for(username: str, now: Optional[float] = None, env: Optional[Mapping[str, str]] = None) -> List[dict]:
    """Items for GET /api/v2/notifications: your voice print switched on, or back to learning."""
    now = time.time() if now is None else now
    out = []
    for a in _alerts(_alerts_path(env)):
        if a.get("user") != username or now - float(a.get("ts") or 0) >= ALERTS_KEEP_S or a.get("state") not in TOLD:
            continue
        title, text = TOLD[a["state"]]
        out.append({"id": a["id"], "kind": "voiceprint", "type": "update", "ts": a["ts"], "title": title, "text": text,
                    "link": "#/settings/voice"})
    return out


async def _tell(app: Any, username: str, state: str) -> None:
    logger.info("voice id: %s's voice print is now %s", username, state)
    try:
        await asyncio.to_thread(record_alert, username, state)
    except OSError as e:
        logger.warning("voice id: could not keep the notification: %s", e)
    try:
        from gateway import push
        store = app.get("push_store") if hasattr(app, "get") else None
        session = app.get("push_session") if hasattr(app, "get") else None
        if store is None or session is None:
            return
        title, text = TOLD[state]
        message = {"title": title, "body": text, "link": "#/settings/voice", "room": "voiceprint"}
        key = store.vapid_key()
        for d in store.devices(username):
            sub = store.get(d["pushkey"])
            if sub:
                await push.send(session, sub, message, key)
    except Exception as e:
        logger.warning("voice id: phone alert failed: %s", type(e).__name__)


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


def _audit(request: Any, user: Mapping[str, Any], action: str, **meta: Any) -> None:
    try:
        import gateway.auth.db as auth_db
        auth_db.write_audit_log(user.get("id") or None, action, target_type="user", target_id=user.get("id") or None,
                                metadata={k: v for k, v in meta.items() if v is not None} or None,
                                ip_address=getattr(request, "remote", None))
    except Exception:
        pass


def _summary(prints: Mapping[str, vp.Profile], username: str) -> dict:
    return prints[username].summary(false_accepts(prints, username))


async def handle_get(request: Any) -> Any:
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    prints = await asyncio.to_thread(load_prints)
    mine = prints.get(user["username"])
    if mine is not None and mine.speaker_threshold is None:     # a print from before VO.3: its speaker strictness
        cohort_s = await speaker_cohort()
        if cohort_s:
            async with _lock():
                prints = await asyncio.to_thread(load_prints)
                mine = prints.get(user["username"])
                if mine is not None:
                    await asyncio.to_thread(recalibrate, prints, user["username"], cohort_s)
                    await asyncio.to_thread(save_print, user["username"], mine)
    return web.json_response({"on": await asyncio.to_thread(_on), "enrolled": mine is not None,
                              "summary": await asyncio.to_thread(_summary, prints, user["username"]) if mine else None,
                              "prompt": PROMPT, "seconds": 25, "need": vp.MIN_ENROL_S,
                              "admin": user.get("role") == "admin",
                              "rules": {"samples": vp.ON_SAMPLES, "days": vp.ON_DAYS, "window": vp.ON_WINDOW,
                                        "rate": vp.ON_RATE, "off": vp.OFF_RATE}})


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
        cohort_s = await speaker_cohort()
        samples = list(got["pieces"]) + ([got["embedding"]] if got.get("embedding") else [])
        async with _lock():
            prints = await asyncio.to_thread(load_prints)
            mine = prints.get(user["username"])
            p = vp.enrol(samples, others, before=mine if request.query.get("add") == "1" else None)
            prints[user["username"]] = p
            await asyncio.to_thread(recalibrate, prints, user["username"], cohort_s)
            await asyncio.to_thread(save_print, user["username"], p)
        logger.info("voice id: %s enrolled (%d samples, strictness %.2f)", user["username"], len(p.samples), p.threshold)
        return web.json_response({"ok": True, "quality": q, "summary": await asyncio.to_thread(_summary, prints, user["username"])})
    problem = vp.problem(q, vp.MIN_TEST_S)
    if problem or not got.get("embedding"):
        return web.json_response({"ok": False, "quality": q, "problem": problem or "Record again, and keep talking."})
    score = mine.score(got["embedding"])
    if step == "other":
        mine.impostors.append(round(score, 4))
        mine.calibrate()
        await asyncio.to_thread(recalibrate, prints, user["username"], await speaker_cohort())
        await asyncio.to_thread(save_print, user["username"], mine)
    # Would the speaker take it for someone else who has a print? (The test is against everyone.)
    state, who, _ = vp.identify(prints | {user["username"]: mine}, got["embedding"], float(q.get("speech") or 0),
                                vp.PHONE, everyone=True)
    return web.json_response({"ok": True, "quality": q, "score": round(score, 2), "threshold": round(mine.threshold, 2),
                              "recognised": score >= mine.threshold, "as": display_name(who) if state == vp.ME else "",
                              "summary": await asyncio.to_thread(_summary, prints, user["username"])})


async def handle_delete(request: Any) -> Any:
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    await asyncio.to_thread(save_print, user["username"], None)
    _held.pop(user["username"], None)
    logger.info("voice id: %s deleted their voice print", user["username"])
    _audit(request, user, "voiceprint_deleted")
    return web.json_response({"ok": True})


async def handle_state(request: Any) -> Any:
    """The person starts learning again (the self-test from nothing; the samples are kept)."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if (body or {}).get("state") != vp.LEARNING:
        return _json_error(400, "state", "Only going back to learning can be asked for: it switches on by itself.")
    async with _lock():
        prints = await asyncio.to_thread(load_prints)
        mine = prints.get(user["username"])
        if mine is None:
            return _json_error(409, "not_enrolled", "Record your voice first.")
        was = mine.state
        mine.reset()
        await asyncio.to_thread(save_print, user["username"], mine)
    _audit(request, user, "voiceprint_reset", was=was)
    logger.info("voice id: %s set their voice print back to learning", user["username"])
    return web.json_response({"ok": True, "summary": await asyncio.to_thread(_summary, prints, user["username"])})


async def handle_heard(request: Any) -> Any:
    """The speaker's last 30 minutes, newest first, scored against my print only (nothing about anyone
    else), with what happened to each: added to my print, held back, or neither."""
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
    held = {h["id"]: h for h in held_for(user["username"], now)}
    thr = mine.threshold_for(vp.SPEAKER)
    out = []
    for r in sorted(rows, key=lambda r: -float(r.get("at") or 0)):
        if not r.get("embedding") or now - float(r.get("at") or 0) > CLAIM_S:
            continue
        s = mine.score(r["embedding"], vp.SPEAKER)
        out.append({"id": r["id"], "at": float(r["at"]), "ago": int(now - float(r["at"])), "speech": r.get("speech"),
                    "score": round(s, 2), "recognised": s >= thr, "added": mine.has_heard(hkey(r)),
                    "held": (held.get(hkey(r)) or {}).get("why")})
    return web.json_response({"heard": out, "threshold": round(thr, 2), "state": mine.state})


async def _row(heard_id: str, need_embedding: bool = True) -> Optional[dict]:
    rows = (await _stt("GET", "/heard")).get("heard") or []
    row = next((r for r in rows if str(r.get("id")) == heard_id and (r.get("embedding") or not need_embedding)), None)
    if row is None or time.time() - float(row["at"]) > CLAIM_S:
        return None
    return row


async def handle_claim(request: Any) -> Any:
    """"That was me": added as a labelled speaker sample. Not when it sounds like someone else who has an
    ACTIVE print, or another person already kept it."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    me = user["username"]
    try:
        row = await _row(request.match_info["id"])
    except Exception:
        return _json_error(503, "stt_unreachable", "The speech-to-text service did not answer.")
    if row is None:
        return _json_error(404, "gone", "That is no longer kept: say something to the speaker again.")
    if float(row.get("speech") or 0) < MIN_SAMPLE_S:
        return _json_error(422, "short", "That was too short to learn from.")
    cohort_s = await speaker_cohort()
    async with _lock():
        prints = await asyncio.to_thread(load_prints)
        mine = prints.get(me)
        if mine is None:
            return _json_error(409, "not_enrolled", "Record your voice first.")
        key = hkey(row)
        if _taken(prints, key) not in (None, me):
            return _json_error(409, "someone_else", "Someone else said that was them, so it was not added.")
        others = {u: p for u, p in prints.items() if u != me}
        state, who, _ = vp.identify(others, row["embedding"], float(row.get("speech") or 0)) if others else (vp.OTHER, None, 0)
        if state == vp.ME:
            return _json_error(409, "someone_else", "That sounded like someone else who has a voice print, so it was not added.")
        if mine.has_heard(key):
            return web.json_response({"ok": True, "score": round(mine.score(row["embedding"], vp.SPEAKER), 2),
                                      "summary": await asyncio.to_thread(_summary, prints, me)})
        st, w, s = vp.identify(prints, row["embedding"], float(row.get("speech") or 0), vp.SPEAKER, everyone=True)
        mine.trials = [t for t in mine.trials if t.get("id") != key]      # a held-back one becomes a real test
        if float(row.get("speech") or 0) >= vp.SHORT_S:
            mine.record_trial(st == vp.ME and w == me, s, key, src="me")
        mine.add_speaker(row["embedding"], row, "me", heard=key)
        _held[me] = [h for h in held_for(me) if h["id"] != key]
        await asyncio.to_thread(recalibrate, prints, me, cohort_s)
        states = await asyncio.to_thread(_review_all, prints, time.time())
        for u in {me} | set(states):
            await asyncio.to_thread(save_print, u, prints[u])
    for u, new in states.items():
        await _tell(request.app, u, new)
    return web.json_response({"ok": True, "score": round(mine.score(row["embedding"], vp.SPEAKER), 2),
                              "summary": await asyncio.to_thread(_summary, prints, me)})


async def handle_not_me(request: Any) -> Any:
    """"Not me": what was added to my print from that command (and its self-test) is taken out."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    me = user["username"]
    try:
        row = await _row(request.match_info["id"], need_embedding=False)
    except Exception:
        return _json_error(503, "stt_unreachable", "The speech-to-text service did not answer.")
    if row is None:
        return _json_error(404, "gone", "That is no longer kept.")
    heard = hkey(row)
    cohort_s = await speaker_cohort()
    async with _lock():
        prints = await asyncio.to_thread(load_prints)
        mine = prints.get(me)
        if mine is None:
            return _json_error(409, "not_enrolled", "Record your voice first.")
        if not mine.remove_heard(heard):
            return _json_error(404, "gone", "Nothing from that was in your voice print.")
        await asyncio.to_thread(recalibrate, prints, me, cohort_s)
        states = await asyncio.to_thread(_review_all, prints, time.time())
        for u in {me} | set(states):
            await asyncio.to_thread(save_print, u, prints[u])
    for u, new in states.items():
        await _tell(request.app, u, new)
    logger.info("voice id: %s took a speaker sample out (not them)", me)
    return web.json_response({"ok": True, "summary": await asyncio.to_thread(_summary, prints, me)})


async def handle_people(request: Any) -> Any:
    """Admin: each person's state and how far along it is. No voices, no scores of single commands."""
    from aiohttp import web
    from gateway import dashboard_api as d
    user = d._user(request)
    if user.get("role") != "admin":
        return _json_error(403, "admin_only", "Only an admin can see this.")
    prints = await asyncio.to_thread(load_prints)
    out = []
    for u in sorted(prints):
        pr = prints[u].progress(false_accepts(prints, u))
        out.append({"username": u, "display_name": display_name(u), "state": prints[u].state,
                    "speaker": len(prints[u].speaker_samples), "labelled": pr["labelled"], "days": pr["days"],
                    "window": pr["window"], "hits": pr["hits"], "held": pr["held"]})
    return web.json_response({"people": out})


def register(app: Any) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/voiceprint", handle_get)
    app.router.add_post("/api/v2/voiceprint", require_csrf(handle_post))
    app.router.add_delete("/api/v2/voiceprint", require_csrf(handle_delete))
    app.router.add_post("/api/v2/voiceprint/state", require_csrf(handle_state))
    app.router.add_get("/api/v2/voiceprint/people", handle_people)
    app.router.add_get("/api/v2/voiceprint/heard", handle_heard)
    app.router.add_post("/api/v2/voiceprint/heard/{id}", require_csrf(handle_claim))
    app.router.add_delete("/api/v2/voiceprint/heard/{id}", require_csrf(handle_not_me))
