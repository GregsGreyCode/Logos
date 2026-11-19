"""logos-waitlist: answer when the model is back (story D2, docs/model-downtime.md).

When LM Studio is closed (Greg is gaming), the model guard answers a model request with a 503
``model_server_off`` after a minute of waiting (story D1). This plugin then:

- tells hermes not to retry that error three more times (each would wait another minute), so
  the person hears at once: "My model is offline right now ... I'll answer this as soon as it's
  back" (the guard's own words);
- remembers the conversation in ``/opt/data/waitlist.json`` (session id and when, no text);
- while anything is waiting, asks the guard every 30 seconds whether LM Studio is back
  (``LOGOS_MODEL_HEALTH_URL``, the sandbox's one rule for it);
- when it is, re-sends each waiting conversation's last message with hermes' own ``/retry``,
  oldest first and one at a time, so LM Studio is not flooded. A message older than 12 hours
  gets a short note instead of an answer to an old question.

Scheduled jobs are not handled here (``rerun_after_outage.py``), nor web chat (the dashboard
re-sends its own messages). Nothing here reads or logs message text.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.request

HOME = os.environ.get("HERMES_HOME", "/opt/data")
STORE = os.path.join(HOME, "waitlist.json")
LOG = os.path.join(HOME, "logs", "waitlist.log")
POLL = 30.0                       # seconds between "is LM Studio back?" while something waits
MAX_AGE = 12 * 3600               # older than this: a note instead of an answer (decided 19 Nov)
TURN_LIMIT = 15 * 60              # longest wait for one re-sent answer before the next
SKIP_PLATFORMS = {"cron", "api", "api_server", "cli", "local", ""}   # handled elsewhere
OFF_CODE = "model_server_off"
EXPIRED_NOTE = ("I was offline when you sent your last message, more than 12 hours ago, so I "
                "haven't answered it. Ask again if you still need it.")

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
                "message": "The local model (LM Studio) is offline; this conversation will be answered when it is back."}
    return None


def _on_error(session_id="", platform="", status_code=None, error=None, **_):
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


async def answer_waiting(gateway, now: float, send_retry, send_note, busy, sleep=asyncio.sleep) -> list:
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
                await send_retry(gateway, origin)
                done.append((session_id, "retried"))
                waited = 0.0
                await sleep(5)
                while await busy(gateway) and waited < TURN_LIMIT:
                    await sleep(5)
                    waited += 5
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


def register(ctx):
    ctx.register_hook("transform_api_error_classification", _classify)
    ctx.register_hook("api_request_error", _on_error)
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
