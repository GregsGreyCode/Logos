"""Bridge between the dashboard's chat stream and an agent's hermes server (S3.5).

The dashboard posts to ``/chat`` and reads ``data: {...}`` lines whose ``type``
is one of ``task_started``, ``token``, ``thinking``, ``tool_start``,
``tool_end``, ``message``, ``done`` or ``error``. Hermes speaks runs and run
events (:mod:`gateway.hermes_client`). :func:`translate` maps one onto the
other and :func:`stream_run` drives a whole turn.

It also keeps the ``agents`` table in step with ``agents/<name>.yaml`` so the
dashboard lists exactly the agents that are defined on this host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

from gateway.hermes_client import HermesClient, HermesError, HermesEvent

logger = logging.getLogger(__name__)

_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def session_id_for(user_id: str, chat_id: str) -> str:
    """Hermes session id for one dashboard conversation.

    Prefixed with the user so two people can never continue each other's
    conversation by guessing a chat id.
    """
    user = _SESSION_SAFE.sub("-", user_id or "anon")[:40]
    chat = _SESSION_SAFE.sub("-", chat_id or "default")[:80]
    return f"web-{user}-{chat}"


class TurnState:
    """What one turn has produced so far (needed to build ``done``)."""

    def __init__(self, model: str = "") -> None:
        self.started = time.monotonic()
        self.model = model
        self.streamed_text = False
        self.tools: List[str] = []
        self._anon_calls = 0
        self._open_anon: List[str] = []

    def call_id(self, ev: HermesEvent, *, opening: bool) -> str:
        """Hermes does not always send a tool call id; pair start/end in order."""
        if ev.tool_call_id:
            return ev.tool_call_id
        if opening:
            self._anon_calls += 1
            cid = f"call-{self._anon_calls}"
            self._open_anon.append(cid)
            return cid
        return self._open_anon.pop(0) if self._open_anon else f"call-{self._anon_calls}"


def _usage(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = raw.get("usage")
    return usage if isinstance(usage, Mapping) else {}


def translate(ev: HermesEvent, state: TurnState) -> List[Dict[str, Any]]:
    """Dashboard events for one hermes event (possibly none)."""
    t = ev.type
    if t == "token":
        if not ev.text:
            return []
        state.streamed_text = True
        return [{"type": "token", "content": ev.text}]
    if t == "reasoning":
        return [{"type": "thinking", "content": ev.text}] if ev.text else []
    if t == "tool_started":
        state.tools.append(ev.tool or "tool")
        return [{"type": "tool_start", "call_id": state.call_id(ev, opening=True),
                 "tool": ev.tool or "tool", "preview": ev.preview}]
    if t in ("tool_completed", "tool_failed"):
        return [{"type": "tool_end", "call_id": state.call_id(ev, opening=False),
                 "success": t == "tool_completed",
                 "duration_ms": int(ev.duration * 1000) if ev.duration is not None else None}]
    if t in ("run_completed", "run_cancelled"):
        # A stopped reply ends like a finished one, so the text already shown is kept.
        out: List[Dict[str, Any]] = []
        if ev.text and not state.streamed_text:
            out.append({"type": "message", "content": ev.text})
        elif t == "run_cancelled" and not state.streamed_text:
            out.append({"type": "message", "content": "(stopped)"})
        usage = _usage(ev.raw)
        out.append({
            "type": "done",
            "stopped": t == "run_cancelled",
            "elapsed_s": round(time.monotonic() - state.started, 1),
            "prompt_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            "tools_used": len(state.tools),
            "model": state.model,
        })
        return out
    if t in ("run_failed", "run_interrupted"):
        return [{"type": "error", "error_type": t, "error_title": "Something went wrong and the agent couldn't finish. Try sending it again.",
                 "content": ev.error or "The run ended without a reply."}]
    return []


def sse(payload: Mapping[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


async def stream_run(
    client: HermesClient,
    message: str,
    *,
    session_id: str,
    model: str = "",
    on_started: Any = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Run one turn and yield dashboard events. Never raises for hermes failures:
    they become a final ``error`` event."""
    state = TurnState(model=model)
    try:
        run_id = await client.start_run(message, session_id=session_id)
    except HermesError as e:
        logger.warning("chat: could not start run: %s", e)
        yield {"type": "error", "error_type": "agent_unreachable",
               "error_title": "Couldn't reach the agent. It may be restarting; try again in a minute.",
               "content": str(e)}
        return
    if on_started:
        on_started(run_id)
    yield {"type": "task_started", "task_id": run_id}
    finished = False
    try:
        async for ev in client.stream_events(run_id):
            for payload in translate(ev, state):
                yield payload
            if ev.terminal:
                finished = True
    except HermesError as e:
        logger.warning("chat: stream for run %s broke: %s", run_id, e)
        yield {"type": "error", "error_type": "stream_broken",
               "error_title": "The connection to the agent dropped. Try sending it again.", "content": str(e)}
        return
    if not finished:
        yield {"type": "error", "error_type": "stream_ended",
               "error_title": "The agent stopped before finishing. Try sending it again.", "content": ""}


# ── agents table <- agents/<name>.yaml ───────────────────────────────────────

def owners_of(agent: Mapping[str, Any]) -> Optional[List[str]]:
    """Usernames allowed to see and talk to this agent, or ``None`` for everyone.

    ``owners: [greg]`` in the definition makes the agent private to those
    accounts. Admins get no exception: an agent holds its person's notes.
    """
    owners = agent.get("owners")
    if owners is None:
        return None
    if isinstance(owners, str):
        owners = [owners]
    return [str(o).strip().lower() for o in owners if str(o).strip()]


def may_use(owners: Optional[List[str]], username: str) -> bool:
    return owners is None or (username or "").strip().lower() in owners


def sync_definitions(runtime: Any, auth_db: Any) -> Dict[str, Optional[List[str]]]:
    """Make sure every agent defined in ``agents/`` has a row in the ``agents``
    table. Returns ``{name: owners}`` for the agents the dashboard may show
    (see :func:`owners_of`).

    A definition with ``dashboard: false`` (for example a benchmark agent) is
    left out.
    """
    shown: Dict[str, Optional[List[str]]] = {}
    for name in runtime.defined():
        try:
            agent = runtime.load(name)
        except Exception as e:
            logger.warning("agent definition %s could not be read: %s", name, e)
            continue
        if agent.get("dashboard") is False:
            continue
        shown[name] = owners_of(agent)
        model = str((agent.get("model") or {}).get("default") or "")
        description = str(agent.get("display_name") or "")
        row = auth_db.get_agent_by_name(name)
        if row is None:
            auth_db.create_agent(name, model=model, description=description, shared=True)
        elif (row.get("model") or "") != model or (row.get("description") or "") != description:
            auth_db.update_agent(row["id"], model=model, description=description)
    return shown


def username_of(user: Mapping[str, Any], auth_db: Any) -> str:
    """Username of the signed-in user (the session token carries only the id)."""
    uid = user.get("sub") or user.get("id") or ""
    if not uid:
        return ""
    try:
        row = auth_db.get_user_by_id(uid)
    except Exception:
        row = None
    return str((row or {}).get("username") or "")


def runtime_of(backend: Any) -> Optional[Any]:
    """The :class:`~gateway.agent_runtime.AgentRuntime` behind ``backend``, if it has one."""
    return getattr(backend, "runtime", None)


KEEP_ALIVE_S = 15


async def keep_alive(write, every: float = KEEP_ALIVE_S) -> None:
    """Write an SSE comment line every few seconds until cancelled.

    A slow model can be silent for minutes before its first word, and a reverse
    proxy in front of Logos drops a stream that is idle for about a minute.
    Browsers ignore comment lines.
    """
    while True:
        await asyncio.sleep(every)
        try:
            await write(b": keep-alive\n\n")
        except ConnectionError:
            return                      # the browser went away; the handler deals with it


MODEL_OFF = "model_off"
MODEL_OFF_MESSAGE = ("The AI computer is off, so no one can answer right now. "
                     "Your message will be sent when it's back on.")


BACKUP_MESSAGE = "The AI computer is off, so a backup model is answering."


def has_backup(definition: Mapping[str, Any]) -> bool:
    """True when the agent's model gateway can answer with a paid model while the local
    one is off: it uses the gateway, names ``model.fallbacks`` and at least one of them
    has a key in ``models.env`` on this machine."""
    from gateway import model_gateway
    if model_gateway.BRAIN_STAYS_LOCAL or not model_gateway.uses_gateway(definition):
        return False
    try:
        text = model_gateway.keys_file(os.environ).read_text()
        return bool(model_gateway.fallback_chain(definition, model_gateway.paid_providers(text)))
    except (OSError, ValueError):
        return False


async def wake_model(model: Mapping[str, Any], http, *, backup: bool = False) -> AsyncIterator[Dict[str, Any]]:
    """Make sure the agent's local model is loaded before a run starts.

    Yields a ``status`` event when it has to load, so the chat can say why the
    reply is slow, or one ``error`` event of type :data:`MODEL_OFF` when the server
    is not answering at all, in which case the caller must not start the run. With
    ``backup`` (see :func:`has_backup`) an unreachable server only yields a ``status``
    event, and the run goes ahead so the gateway's fallback answers. Problems are logged and swallowed: the model server loads on
    demand anyway, so the run is still worth starting.
    """
    from gateway import local_models
    base = local_models.server(model)
    if not base or not model.get("default"):
        return
    if not await asyncio.to_thread(local_models.reachable, http, base):
        # Starting a run now would leave the person watching "Thinking" for many minutes
        # while the agent waits on a server that is not there. Say so at once instead,
        # unless the model gateway has a paid fallback to answer in its place.
        if backup:
            yield {"type": "status", "content": BACKUP_MESSAGE}
            return
        yield {"type": "error", "error_type": MODEL_OFF, "error_title": MODEL_OFF_MESSAGE}
        return
    try:
        have = await asyncio.to_thread(local_models.loaded, http, base)
        if local_models.is_ready(have, model["default"], model.get("context_length") or 0):
            return
        yield {"type": "status", "content": "Loading the model. The first reply after a quiet spell takes longer."}
        await asyncio.to_thread(local_models.ensure, http, base, model["default"],
                                model.get("context_length") or 0)
    except Exception as exc:
        logger.warning("chat: could not check or load the local model: %s", exc)
