"""logos-waitlist: answer when the model is back (story D2, docs/model-downtime.md).

When LM Studio is closed (Greg is gaming), the model guard answers a model request with a 503
``model_server_off`` after a minute of waiting (story D1). This plugin then:

- tells hermes not to retry that error three more times (each would wait another minute), so
  the person hears at once: "I can't answer right now: the AI computer is off ..." (the
  guard's own words). In the household group chat, once model-watch has told everyone, the
  household agent answers each message with one short line instead, so the explanation is not
  repeated under every message (docs/messages.md);
- remembers the conversation in ``/opt/data/waitlist.json`` (session id and when, no text);
- while anything is waiting, asks the guard every 30 seconds whether LM Studio is back
  (``LOGOS_MODEL_HEALTH_URL``, the sandbox's one rule for it);
- when it is, re-sends each waiting conversation's last message with hermes' own ``/retry``,
  oldest first and one at a time, so LM Studio is not flooded. A message older than 12 hours
  gets a short note instead of an answer to an old question.
- checks that the re-sent turn got its answer through. A model just loaded can loop in its
  thinking (21 Nov: twelve minutes, then hermes cut it and Greg read "Your request was not
  processed"), so a re-sent turn that failed for any reason but the model being off again is
  sent once more. Only the transcript's last row is read (its role and failure mark, no text).

- in the chat, replaces hermes's own words for that failed turn ("... temporarily unavailable ...
  add a backup provider ... Your request was not processed. Send it again ...") with the one
  line above: the person need not send it again, and there is no backup provider by design
  (the brain stays local). The line is hermes's failed-turn notice, so the model never reads it.

- tells the agent, at the start of its next turn in that conversation, that its last reply failed
  and why. hermes strips its own failure notice before the model reads the conversation, so
  without this an agent asked "what happened?" guessed ("a formatting error") or redid the
  request without saying anything (evals: failed_turn_told_plainly_and_redone).

Scheduled jobs are not handled here (``rerun_after_outage.py``), nor web chat (the dashboard
re-sends its own messages). Nothing here reads or logs message text.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import urllib.request

HOME = os.environ.get("HERMES_HOME", "/opt/data")
STORE = os.path.join(HOME, "waitlist.json")
LOG = os.path.join(HOME, "logs", "waitlist.log")
POLL = 30.0                       # seconds between "is LM Studio back?" while something waits
MAX_AGE = 12 * 3600               # older than this: a note instead of an answer (decided 19 Nov)
TURN_LIMIT = 15 * 60              # longest wait for one re-sent answer before the next
RESENDS = 2                       # tries per waiting conversation: the re-send, and once more if it failed
DB = os.path.join(HOME, "state.db")
SKIP_PLATFORMS = {"cron", "api", "api_server", "cli", "local", ""}   # handled elsewhere
OFF_CODE = "model_server_off"
EXPIRED_NOTE = ("The AI computer was off when you sent your last message, more than 12 hours ago, "
                "so I didn't answer it. Ask again if you still need it.")
OFF_REPLY = "I can't answer right now: the AI computer is off. I'll reply here as soon as it's back on."
# After model-watch's group notice (household agent only; its state file is in the same home).
NOTED_REPLY = "Noted. I'll answer this when the AI computer is back on."
WATCH_STATE = os.path.join(HOME, ".model_watch")


def off_reply() -> str:
    """The short line once the group has been told the AI computer is off; the full one otherwise."""
    try:
        with open(WATCH_STATE) as f:
            if json.load(f).get("told"):
                return NOTED_REPLY
    except (OSError, ValueError, AttributeError):
        pass
    return OFF_REPLY

_lock = threading.Lock()
_state = {"gateway": None, "loop": None, "task": None}


def _log(event: str, **fields) -> None:
    fields.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), event=event)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, default=str) + "\n")
    except OSError:
        pass


def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(waiting: dict) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(waiting, f)
    os.replace(tmp, STORE)


def is_off(*texts) -> bool:
    return any(OFF_CODE in str(t or "") for t in texts)


def add(session_id: str, platform: str, now: float) -> bool:
    """Remember a conversation that could not be answered. True when it was not waiting yet."""
    if not session_id or platform in SKIP_PLATFORMS:
        return False
    with _lock:
        waiting = load()
        if session_id in waiting:
            return False
        waiting[session_id] = {"platform": platform, "since": now}
        save(waiting)
    return True


def model_back(url: str = "") -> bool:
    url = url or os.environ.get("LOGOS_MODEL_HEALTH_URL", "")
    if not url:
        try:
            env = dict(l.strip().split("=", 1) for l in open(os.path.join(HOME, ".env"))
                       if "=" in l and not l.startswith("#"))
            url = env.get("LOGOS_MODEL_HEALTH_URL", "")
        except OSError:
            return False
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return not json.load(r).get("model_server_off", True)
    except Exception:
        return False


# ── hooks ────────────────────────────────────────────────────────────────────

def _classify(status_code=None, error_code="", error_message="", error_body=None, **_):
    """The guard's "model off" is final for this turn: no retries, the guard's words to the person."""
    if status_code == 503 and is_off(error_code, error_message, json.dumps(error_body or {})):
        return {"reason": "overloaded", "retryable": False, "should_fallback": False,
                "message": off_reply()}
    return None


def _on_error(session_id="", platform="", status_code=None, error=None, **_):
    _remember_failure(session_id, status_code, error)
    if status_code != 503 or not is_off((error or {}).get("message")):
        return
    if add(session_id, str(platform or ""), time.time()):
        _log("waiting", session=session_id[:16], platform=platform)
    loop = _state["loop"]
    if loop is not None:
        loop.call_soon_threadsafe(_ensure_watcher)


async def _on_dispatch(event=None, gateway=None, **_):
    if gateway is not None:
        _state["gateway"] = gateway
        _state["loop"] = asyncio.get_running_loop()
        _ensure_watcher()
    return None


# ── the watcher ──────────────────────────────────────────────────────────────

def _ensure_watcher() -> None:
    task = _state["task"]
    if (task is None or task.done()) and load() and _state["gateway"] is not None:
        _state["task"] = asyncio.get_running_loop().create_task(_watch())


async def _busy(gateway) -> bool:
    return bool(getattr(gateway, "_running_agents", None))


def last_turn_failed(session_id: str, db: str = "") -> str:
    """Why the conversation's last turn failed, in plain words; "" when it was answered.

    hermes closes a failed turn with an assistant row marked ``failed_turn`` (its metadata holds
    the error). Only that row's role and marks are read, never message text."""
    try:
        con = sqlite3.connect(f"file:{db or DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT role, display_kind, display_metadata FROM messages "
                              "WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""                     # cannot tell: do not send it again on a guess
    if not row or row[0] != "assistant" or row[1] != "failed_turn":
        return ""
    try:
        error = str((json.loads(row[2] or "{}") or {}).get("error") or "")
    except (ValueError, AttributeError):
        error = ""
    return why(error={"message": error})


async def _wait_for_turn(gateway, busy, sleep) -> None:
    waited = 0.0
    await sleep(5)
    while await busy(gateway) and waited < TURN_LIMIT:
        await sleep(5)
        waited += 5


async def answer_waiting(gateway, now: float, send_retry, send_note, busy, sleep=asyncio.sleep,
                         failed=last_turn_failed) -> list:
    """Answer every waiting conversation, oldest first, one at a time. Returns what was done."""
    done = []
    for session_id, item in sorted(load().items(), key=lambda kv: kv[1].get("since", 0)):
        # Off the list first: if the model drops out again during the answer, the error hook
        # puts it back, and that must not be undone here afterwards.
        with _lock:
            waiting = load()
            waiting.pop(session_id, None)
            save(waiting)
        origin = gateway._gateway_session_origin_for_id(session_id)
        try:
            if origin is None:
                done.append((session_id, "gone"))
            elif now - float(item.get("since", now)) > MAX_AGE:
                await send_note(gateway, origin)
                done.append((session_id, "too old"))
            else:
                outcome = "retried"
                for attempt in range(1, RESENDS + 1):
                    await send_retry(gateway, origin)
                    await _wait_for_turn(gateway, busy, sleep)
                    if session_id in load():
                        outcome = "off again"        # the error hook put it back on the list
                        break
                    cause = failed(session_id)
                    if cause == why(error={"message": OFF_CODE}):
                        outcome = "off again"
                        break
                    if not cause:
                        outcome = "retried" if attempt == 1 else "retried twice"
                        break
                    outcome = f"failed: {cause}"
                done.append((session_id, outcome))
        except Exception as e:
            done.append((session_id, f"failed: {type(e).__name__}"))
    return done


async def _send_retry(gateway, origin) -> None:
    from gateway.platforms.event import MessageEvent, MessageType
    adapter = gateway._delivery_adapter_for(origin)
    if adapter is None:
        raise RuntimeError("no adapter")
    await adapter.handle_message(MessageEvent(text="/retry", message_type=MessageType.TEXT, source=origin))


async def _send_note(gateway, origin) -> None:
    adapter = gateway._delivery_adapter_for(origin)
    if adapter is None:
        raise RuntimeError("no adapter")
    await adapter.send(origin.chat_id, EXPIRED_NOTE)


async def _watch() -> None:
    gateway = _state["gateway"]
    _log("watching", waiting=len(load()))
    while load():
        await asyncio.sleep(POLL)
        if not await asyncio.to_thread(model_back):
            continue
        _log("model back", waiting=len(load()))
        for session_id, outcome in await answer_waiting(gateway, time.time(), _send_retry, _send_note, _busy):
            _log("answered", session=session_id[:16], outcome=outcome)


# ── the chat line for a turn that failed because the model is off ────────────

OFF_MARKS = (OFF_CODE, "the AI computer is off", "LM Studio isn't running")


def off_failure(*texts) -> bool:
    """True when a failed turn's text says the model server is off (the guard's words or code)."""
    return any(mark in str(t or "") for t in texts for mark in OFF_MARKS)


def patch_failed_turn_notice() -> bool:
    """For a turn that failed because the model is off, the chat shows only :func:`off_reply`."""
    try:
        from gateway.run_turn_persistence import GatewayTurnPersistenceMixin as Mixin
    except Exception:
        return False
    original = Mixin._hmwa_add_failed_turn_notice
    if getattr(original, "_logos_waitlist", False):
        return True

    def _hmwa_add_failed_turn_notice(self, response, notice):
        if off_failure(response):
            return off_reply()
        return original(self, response, notice)

    _hmwa_add_failed_turn_notice._logos_waitlist = True
    Mixin._hmwa_add_failed_turn_notice = _hmwa_add_failed_turn_notice
    return True


# ── a note for the next turn after a failed one ───────────────────────────────

FAILED: dict = {}                 # session id -> {"why": ..., "at": ...}; a success clears it
NOTE_MAX_AGE = 12 * 3600
FAILED_NOTE = ("[Note from Logos, not from your person: your previous reply in this conversation failed "
               "before it reached them ({why}); they only saw \"Your request was not processed\". "
               "If they ask what happened, start with one plain sentence saying so. Then, in this same "
               "reply, do what they asked before it failed, without asking them to send it again.]")


def why(status_code=None, error=None) -> str:
    """The failure in a few plain words, from what hermes reports."""
    raw = str((error or {}).get("message") or "") + " " + str((error or {}).get("type") or "")
    text = raw.lower()
    if is_off(raw) or off_failure(raw):
        return "the AI computer was off"
    if "repetition" in text or "runaway" in text:
        return "the model got stuck repeating itself and was stopped"
    if "context size" in text or "context length" in text or "too long" in text:
        return "the conversation was too long for the model at that moment"
    if "timeout" in text or "timed out" in text or "stale" in text:
        return "the model took too long to answer"
    return "the model did not answer"


def _remember_failure(session_id="", status_code=None, error=None, **_):
    if session_id:
        FAILED[session_id] = {"why": why(status_code, error), "at": time.time()}


def _forget_failure(session_id="", **_):
    FAILED.pop(session_id, None)


def _note_for_next_turn(session_id="", **_):
    item = FAILED.pop(session_id, None) if session_id else None
    if item and time.time() - item["at"] < NOTE_MAX_AGE:
        return {"context": FAILED_NOTE.format(why=item["why"])}
    return None


def register(ctx):
    patch_failed_turn_notice()
    ctx.register_hook("post_api_request", _forget_failure)
    ctx.register_hook("pre_llm_call", _note_for_next_turn)
    ctx.register_hook("transform_api_error_classification", _classify)
    ctx.register_hook("api_request_error", _on_error)
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
