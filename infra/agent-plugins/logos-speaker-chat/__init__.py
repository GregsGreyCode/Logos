"""logos-speaker-chat: what is said through the house speaker shows in the agent's chat (story V1.3).

The house speaker reaches the agent through Logos (gateway/voice_converse.py), which runs it over
hermes's HTTP API in a session of its own per speaker per day (``voice-<agent>-<device>-<day>``). That
exchange used to live only there: the people's Matrix room never saw it, and "I'll put the answer in
our chat" was not true.

As such a turn finishes, this posts it in the agent's main Matrix room (``MATRIX_HOME_ROOM``, where its
scheduled messages go; for Home, the room Greg and Béré share), through the agent's own Matrix client,
so it is end-to-end encrypted like any of its replies:

    🔊 Heard at the house speaker:
    > “What's on tomorrow?”

    The dentist at ten, then nothing until the evening.

- **It is the agent's message**, not the person's: the words heard are quoted inside it. Who spoke is
  not known yet (voice prints come later), so it says only that it was the house speaker.
- **The full answer**, not the shortened spoken one, with ``org.logos.speaker`` ({heard, late}) in the
  message for the Logos chat to show it as a speaker exchange. Element X shows the text.
- **A notice when it was answered out loud** (``m.notice``: a record, which does not count as unread
  and which agents do not answer); **a normal message when it came too late** for the speaker (it said
  the answer would be in the chat), so it is noticed.
- **The chat conversation is told** (hermes's own delivery mirror, as for scheduled messages), so "what
  did I just ask the speaker?" in the chat has an answer.
- Only this agent's speaker sessions: the nightly timing check (``voice-probe-…``) is not posted.
- It never delays the answer: the post goes on the gateway's loop in the background, and any failure
  is only logged. Nothing here logs what was said.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
import time

logger = logging.getLogger("logos.speaker_chat")

HOME = os.environ.get("HERMES_HOME", "/opt/data")
AGENT_ENV, ROOM_ENV = "LOGOS_AGENT_NAME", "MATRIX_HOME_ROOM"
KEY = "org.logos.speaker"
HEADER = "🔊 Heard at the house speaker:"
SPEAKER_WAIT_S = 42.0       # voice_converse.py WAIT_S (45) less the time before the run starts here
MAX_HEARD = 2000            # voice_converse MAX_TEXT
MAX_ANSWER = 20000          # well inside a Matrix event (64 KB)
KEEP_S = 3600

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_started: dict = {}         # turn_id -> monotonic start
_lock = threading.Lock()


def settings() -> dict:
    """The agent's name and home room: from the environment, else its .env."""
    got = {k: os.environ.get(k, "") for k in (AGENT_ENV, ROOM_ENV)}
    if all(got.values()):
        return got
    try:
        with open(os.path.join(HOME, ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in got and not got[k]:
                    got[k] = v
    except OSError:
        pass
    return got


def is_speaker_session(session_id, agent: str) -> bool:
    """``voice-<agent>-<device>-<day>`` (voice_converse.session_for); not the nightly check."""
    sid = str(session_id or "")
    return bool(agent) and sid.startswith(f"voice-{_SAFE.sub('-', agent)}-")


def heard_of(text) -> str:
    return " ".join(str(text or "").split())[:MAX_HEARD]


def content_for(heard: str, answer: str, late: bool, to_html=None) -> dict:
    """The Matrix message: the words heard, quoted, then the answer."""
    heard = heard_of(heard)
    answer = str(answer or "").strip()[:MAX_ANSWER]
    body = f"{HEADER}\n> “{heard}”\n\n{answer}"
    answer_html = ""
    if to_html is not None:
        try:
            answer_html = to_html(answer) or ""
        except Exception:
            answer_html = ""
    if not answer_html:
        answer_html = "<p>" + html.escape(answer).replace("\n", "<br>") + "</p>"
    formatted = (f"<p>🔊 <em>Heard at the house speaker:</em></p>"
                 f"<blockquote>“{html.escape(heard)}”</blockquote>{answer_html}")
    return {"msgtype": "m.text" if late else "m.notice", "body": body,
            "format": "org.matrix.custom.html", "formatted_body": formatted,
            KEY: {"heard": heard, "late": bool(late)}}


def mirror_text(heard: str, answer: str) -> str:
    """What the chat conversation is told (as a note in the people's turn, like a scheduled message)."""
    return ("[Logos: this was said to you through the house speaker a moment ago; who said it is not "
            f"known. It is also posted in this chat.] Heard: “{heard_of(heard)}” You answered: "
            f"“{' '.join(str(answer or '').split())[:4000]}”")


def _runner():
    try:
        import gateway.run as gr                      # the module global is replaced when the gateway starts
        return gr._gateway_runner_ref()
    except Exception:
        return None


def _matrix_adapter(runner):
    for adapter in list((getattr(runner, "adapters", None) or {}).values()):
        if type(adapter).__name__ == "MatrixAdapter" or str(getattr(getattr(adapter, "platform", None), "value", "")) == "matrix":
            return adapter
    return None


async def _send(adapter, room: str, content: dict) -> str:
    try:
        return await adapter._send_room_message(room, content)
    except Exception:
        crypto = getattr(getattr(adapter, "_client", None), "crypto", None)
        if crypto is None:
            raise
        await crypto.share_keys()                      # as the adapter's own send: once more after sharing keys
        return await adapter._send_room_message(room, content)


def room_session(runner, room: str):
    """The room's own conversation (not one of its threads): session key ``agent:<profile>:matrix:<dm|group>:<room>``.
    Named exactly: hermes's own search by room also finds the threads, and can pick one of those."""
    store = getattr(runner, "session_store", None)
    try:
        entries = store.list_sessions() if store is not None else []
    except Exception:
        return None
    for e in entries:                                   # newest first
        key = str(getattr(e, "session_key", "") or "")
        if ":matrix:" in key and key.endswith(f":{room}") and getattr(e, "session_id", None):
            return str(e.session_id)
    return None


def _mirror(runner, room: str, heard: str, answer: str) -> None:
    try:
        from gateway.mirror import mirror_to_session
        sid = room_session(runner, room)
        ok = bool(sid) and mirror_to_session("matrix", room, mirror_text(heard, answer), source_label="logos-speaker-chat",
                                             role="user", session_id=sid)
        logger.info("speaker chat: %s", "the chat conversation was told" if ok else "no chat conversation to tell yet")
    except Exception as e:
        logger.info("speaker chat: could not tell the chat conversation (%s)", type(e).__name__)


def post(heard: str, answer: str, late: bool, conf=None, runner=None) -> bool:
    """Hand the post to the gateway's loop; never waits for it."""
    conf = conf or settings()
    room = conf.get(ROOM_ENV, "")
    if not room:
        logger.info("speaker chat: this agent has no home room; not posted")
        return False
    runner = runner if runner is not None else _runner()
    adapter = _matrix_adapter(runner) if runner is not None else None
    loop = getattr(runner, "_gateway_loop", None)
    if adapter is None or loop is None or not loop.is_running():
        logger.info("speaker chat: Matrix is not connected; not posted")
        return False
    content = content_for(heard, answer, late, getattr(adapter, "_markdown_to_html", None))
    fut = asyncio.run_coroutine_threadsafe(_send(adapter, room, content), loop)

    def done(f):
        e = f.exception()
        if e is not None:
            logger.warning("speaker chat: the post failed (%s)", type(e).__name__)
            return
        logger.info("speaker chat: posted in the home room%s", " (late answer)" if late else "")
        threading.Thread(target=_mirror, args=(runner, room, heard, answer), daemon=True, name="logos-speaker-mirror").start()

    fut.add_done_callback(done)
    return True


def started(session_id=None, turn_id=None, **_):
    if turn_id is None or not is_speaker_session(session_id, settings().get(AGENT_ENV, "")):
        return None
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _started.items() if now - t > KEEP_S]:
            del _started[k]
        _started[turn_id] = now
    return None


def finished(session_id=None, turn_id=None, user_message=None, assistant_response=None, **_):
    conf = settings()
    if not is_speaker_session(session_id, conf.get(AGENT_ENV, "")):
        return None
    with _lock:
        t0 = _started.pop(turn_id, None)
    answer = assistant_response if isinstance(assistant_response, str) else ""
    heard = heard_of(user_message if isinstance(user_message, str) else "")
    if not heard or not answer.strip():
        logger.info("speaker chat: nothing to post (no %s)", "words" if not heard else "answer")
        return None
    late = t0 is not None and time.monotonic() - t0 >= SPEAKER_WAIT_S
    try:
        post(heard, answer, late, conf)
    except Exception as e:                              # never the turn's problem
        logger.warning("speaker chat: not posted (%s)", type(e).__name__)
    return None


def register(ctx):
    ctx.register_hook("pre_llm_call", started)
    ctx.register_hook("post_llm_call", finished)
