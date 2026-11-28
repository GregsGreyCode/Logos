"""Settings › Household words, and getting the list to every speech-to-text path (28 Nov).

* ``GET /api/v2/household-words``: ``{words: [{word, heard_as, note?, source?}]}``: the saved words,
  then the ones that join by themselves (``source``: ``person`` or ``agent``).
* ``PUT /api/v2/household-words`` ``{words: [...]}``: the whole saved list. Anyone signed in to the
  household may change it, as with the shopping list. Words with nothing heard and no note that
  would join by themselves anyway are not kept.

The list itself is :mod:`gateway.household_words`. Every minute, and at once after a change,
:func:`sync` sends it where it is used, each only when it changed (the house speaker's
speech-to-text every time: it keeps the list in memory only):

- the house speaker's speech-to-text (``logos-stt``, its HTTP door on 127.0.0.1, with its token);
- the voice service for voice notes and dictation (``logos-voice``: ``words.json`` in its volume);
- every agent (``logos-words.json`` in its home, read by the ``logos-household-words`` plugin on
  each turn: one line, "Names here: …").

No container is restarted for a change.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Callable, Dict, List, Mapping, Optional

from gateway import household_words as hw

logger = logging.getLogger(__name__)

PATH = "/api/v2/household-words"
INTERVAL_S = 60
_TASKS: set = set()
AGENT_FILE = "logos-words.json"          # in each agent's home, read by infra/agent-plugins/logos-household-words


# ── the words that join by themselves ────────────────────────────────────────────────────────

def people_words(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Each person's first name (the first word of their display name). Blocking; [] on trouble."""
    from gateway import agent_runtime as ar
    rows: List[Mapping[str, Any]] = []
    try:
        import gateway.auth.db as auth_db
        rows = list(auth_db.list_users(page=1, limit=1000)[0])
    except Exception as e:
        logger.info("household words: the accounts could not be read (%s)", type(e).__name__)
    try:
        rows += ar.household_members(os.environ if env is None else env)
    except Exception:
        pass
    out = []
    for r in rows:
        shown = str(r.get("display_name") or "").split()
        if shown and shown[0] not in out:
            out.append(shown[0])
    return out


def agent_words(agents: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """What the agents are called aloud: a name of their own ("Wren", not "Wren's agent" or
    "Household agent") and the name the speaker wakes to."""
    out: List[str] = []
    for a in agents.values():
        shown = str(a.get("display_name") or "").strip()
        names = [str(a.get("speaker_name") or "").strip()]
        if shown and len(shown.split()) == 1 and "'" not in shown and "’" not in shown:
            names.insert(0, shown)
        for n in names:
            if n and n not in out:
                out.append(n)
    return out


def _agents(runtime: Any) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for name in runtime.defined() if runtime is not None else []:
        try:
            out[name] = runtime.load(name)
        except Exception:
            continue
    return out


def auto(runtime: Any) -> List[Dict[str, Any]]:
    env = runtime.env if runtime is not None else os.environ
    return hw.defaults(people_words(env), agent_words(_agents(runtime)))


def listing(runtime: Any, env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Blocking: the saved words, then the automatic ones."""
    env = (runtime.env if runtime is not None else os.environ) if env is None else env
    return hw.merged(hw.load(hw.path(env)), auto(runtime))


def keepable(entries: Any, automatic: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """What a PUT keeps: an automatic word only once something was added to it."""
    joined = {e["word"].casefold() for e in automatic}
    return [e for e in hw.clean(entries) if e["heard_as"] or e.get("note") or e["word"].casefold() not in joined]


# ── sending it where it is used ──────────────────────────────────────────────────────────────

def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sync(runtime: Any, sent: Dict[str, str], env: Optional[Mapping[str, str]] = None,
         stt_send: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
         voice_write: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
         agent_volume: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    """Blocking. Send the list where it is used; ``sent`` (kept by the caller) remembers what each
    place last got, so only changes go out. Returns what was done, for the log and the tests."""
    env = (runtime.env if runtime is not None else os.environ) if env is None else env
    entries = [{k: v for k, v in e.items() if k != "source"} for e in listing(runtime, env)]
    done: Dict[str, Any] = {"words": len(entries)}
    digest = _digest(entries)

    if stt_send is None:
        from gateway import stt_host
        try:
            on = stt_host.settings(env) is not None
        except Exception:
            on = False
        stt_send = (lambda e: stt_host.send_words(e, env)) if on else None
    if stt_send is not None:                        # every time: a restarted container has nothing
        try:
            stt_send(entries)
            done["stt"] = True
        except Exception as e:
            done["stt"] = False
            if sent.get("stt") != "failed":
                logger.info("household words: the house speaker's speech-to-text did not take them (%s)", type(e).__name__)
            sent["stt"] = "failed"
        else:
            sent["stt"] = digest

    if sent.get("voice") != digest:
        if voice_write is None and runtime is not None:
            from gateway import voice_host
            voice_write = voice_host.VoiceHost(runtime).write_words
        try:
            done["voice"] = bool(voice_write(entries)) if voice_write else False
            if done["voice"]:                       # no voice service yet: try again next time
                sent["voice"] = digest
        except Exception as e:
            done["voice"] = False
            logger.info("household words: the voice service did not take them (%s)", type(e).__name__)

    note = hw.agent_note(entries)
    body = json.dumps({"text": note}, ensure_ascii=False)
    if agent_volume is None and runtime is not None:
        agent_volume = lambda vol: runtime.runner.run(["docker", "volume", "inspect", vol]).returncode == 0  # noqa: E731
    written = []
    for name in (runtime.defined() if runtime is not None else []):
        key = f"agent:{name}"
        if sent.get(key) == _digest(body):
            continue
        vol = f"logos-home-{name}"
        try:
            if agent_volume and not agent_volume(vol):   # `docker run -v` would make an empty one
                continue
            runtime.put(vol, AGENT_FILE, body, mode="0644")
            sent[key] = _digest(body)
            written.append(name)
        except Exception as e:
            logger.info("household words: %s's note was not written (%s)", name, type(e).__name__)
    done["agents"] = written
    return done


async def sync_now(app: Any) -> None:
    """Send the list now (one at a time); never raises."""
    async with app["household_words_lock"]:
        try:
            done = await asyncio.to_thread(sync, _runtime(app), app["household_words_sent"])
            if done.get("agents") or done.get("voice"):
                logger.info("household words: %s words sent (voice notes: %s; agents: %s)", done["words"],
                            "yes" if done.get("voice") else "no", ", ".join(done["agents"]) or "none")
        except Exception as e:
            logger.warning("household words: could not send them: %s", e)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _runtime(app: Any) -> Any:
    from gateway import agent_chat
    return agent_chat.runtime_of(app.get("sandbox_backend"))


def _signed_in(request: Any) -> Dict[str, str]:
    from aiohttp import web
    from gateway.dashboard_api import _user
    user = _user(request)
    if not user["id"]:
        raise web.HTTPUnauthorized()
    return user


async def handle_get(request: Any) -> Any:
    from aiohttp import web
    _signed_in(request)
    words = await asyncio.to_thread(listing, _runtime(request.app))
    return web.json_response({"words": words, "max": hw.MAX_WORDS})


async def handle_put(request: Any) -> Any:
    from aiohttp import web
    user = _signed_in(request)
    try:
        body = await request.json()
        raw = body["words"]
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request", "message": "Send the list as words."}, status=400)
    if not isinstance(raw, list) or len(raw) > hw.MAX_WORDS:
        return web.json_response({"error": "too_many", "message": f"{hw.MAX_WORDS} words at most."}, status=400)
    runtime = _runtime(request.app)
    env = runtime.env if runtime is not None else os.environ

    def save() -> List[Dict[str, Any]]:
        automatic = auto(runtime)
        hw.save(hw.path(env), keepable(raw, automatic))
        return hw.merged(hw.load(hw.path(env)), automatic)

    words = await asyncio.to_thread(save)
    try:
        import gateway.auth.db as auth_db
        auth_db.write_audit_log(user["id"] or None, "household_words_changed", target_type="settings",
                                target_id="household-words", metadata={"words": len(words)})
    except Exception:
        pass
    task = asyncio.create_task(sync_now(request.app))       # at once, not in a minute
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({"words": words, "max": hw.MAX_WORDS})


def register(app: Any) -> None:
    from gateway.auth.middleware import require_csrf
    app["household_words_sent"] = {}             # what each place last got (sync)
    app["household_words_lock"] = asyncio.Lock()
    app.router.add_get(PATH, handle_get)
    app.router.add_put(PATH, require_csrf(handle_put))

    async def _start(_app: Any) -> None:
        async def _loop() -> None:
            await asyncio.sleep(20)                     # after the boot's own work
            while True:
                await sync_now(_app)
                await asyncio.sleep(INTERVAL_S)
        _app["household_words_loop"] = asyncio.create_task(_loop())

    async def _stop(_app: Any) -> None:
        task = _app.get("household_words_loop")
        if task:
            task.cancel()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
