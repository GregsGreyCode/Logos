"""logos-reactions: emoji reactions on an agent's messages reach the agent (Greg, 22 Nov).

hermes's Matrix adapter acts on a reaction only when it lands on one of its own prompts (an
approval, the model picker, a choice picker) and drops every other one, so a 👎 under a wrong
answer told the agent nothing. With this plugin a reaction on one of the agent's own messages:

- is told to the agent at the start of its next turn in that conversation, with the start of the
  message it was on ("Greg reacted 👍 to your message: "…""). The agent does not reply to it:
  a reply to every 👍 would be noise and would hold one of the AI computer's two slots;
- for 👎 and ❤️, also adds a dated line to ``topics/lessons.md`` in the agent's brain, so the
  nightly consolidation and the weekly digest learn from it (judgement.md);
- for ❓ and 🔁, starts a turn, as if the person had replied to that message "Explain that
  more." or "Try that again a different way." (docs/messages.md lists them);
- answers ``/emojis`` (or ``/reactions``) with that list, without the model.

Only messages the agent sent since this plugin was installed are known: Matrix sends are now
recorded in hermes's own sent-message index (``rich_sent_store``, which Telegram already uses),
and a reaction on anything not in it is left alone. A reaction taken away again is not told.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

logger = logging.getLogger("logos.reactions")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"
HOME = os.environ.get("HERMES_HOME", "/opt/data")
LESSONS = os.path.join(HOME, "brain", "topics", "lessons.md")
LOG = os.path.join(HOME, "logs", "reactions.log")
EXCERPT = 120                  # characters of the reacted message quoted to the agent
NOTE_MAX = 6                   # notes kept per conversation until its next turn
NOTE_MAX_AGE = 24 * 3600

COMMANDS = {"❓": "Explain that more.", "❔": "Explain that more.",
            "🔁": "Try that again a different way.", "🔄": "Try that again a different way."}
LESSON_WORDS = {"👎": "didn't like", "❤️": "loved"}
LESSONS_HEAD = "# Lessons\n\nHow I work better: rules from what went wrong, and reactions to work out.\n\n"

HELP = ("React to any of my messages and I'll see it the next time we talk. I don't reply to a reaction, "
        "except these:\n"
        "- ❓ explain that more\n"
        "- 🔁 try that again a different way\n"
        "- 👎 that wasn't right: I note it and learn from it\n"
        "- ❤️ that was just right: I note that too\n"
        "Any other emoji, like 👍, just tells me how it landed.")

NOTES: dict = {}               # session id -> [{"text", "at"}]
_lock = threading.Lock()


def _log(event: str, **fields) -> None:
    fields.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), event=event)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, default=str) + "\n")
    except OSError:
        pass


def plain(key: str) -> str:
    """The emoji without variation selectors or skin tones: '👍🏽' -> '👍', '❤' -> '❤️'."""
    key = "".join(c for c in (key or "") if c != "️" and not 0x1F3FB <= ord(c) <= 0x1F3FF).strip()
    return "❤️" if key == "❤" else key


def excerpt(text: str, limit: int = EXCERPT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def note_text(name: str, key: str, text: str) -> str:
    line = f'{name} reacted {key} to your message: "{excerpt(text)}".'
    if plain(key) in LESSON_WORDS:
        line += " It is noted in topics/lessons.md."
    return line + " A reaction needs no reply; let it guide what you say next."


def add_note(session_id: str, text: str, now: float) -> None:
    if not session_id:
        return
    with _lock:
        notes = [n for n in NOTES.get(session_id, []) if now - n["at"] < NOTE_MAX_AGE]
        notes.append({"text": text, "at": now})
        NOTES[session_id] = notes[-NOTE_MAX:]


def _note_for_next_turn(session_id="", **_):
    with _lock:
        notes = NOTES.pop(session_id, None) if session_id else None
    fresh = [n["text"] for n in notes or [] if time.time() - n["at"] < NOTE_MAX_AGE]
    return {"context": "\n".join(fresh)} if fresh else None


def write_lesson(name: str, key: str, text: str, path: str = "", today: str = "") -> bool:
    words = LESSON_WORDS.get(plain(key))
    if not words:
        return False
    path = path or LESSONS
    today = today or time.strftime("%Y-%m-%d")
    line = f'- {today}: {name} {words} this reply ({plain(key)} reaction): "{excerpt(text)}"\n'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write((LESSONS_HEAD if new else "") + line)
        return True
    except OSError as e:
        logger.warning("reactions: could not write lessons.md: %s", e)
        return False


# ── what the agent sent ──────────────────────────────────────────────────────

def _sent_store():
    from gateway import rich_sent_store
    return rich_sent_store


async def _record(chat_id, event_id, text) -> None:
    try:
        await _sent_store().record_async(str(chat_id), str(event_id), text)
    except Exception:
        pass


def _lookup(chat_id, event_id):
    try:
        return _sent_store().lookup(str(chat_id), str(event_id))
    except Exception:
        return None


# ── the reaction ─────────────────────────────────────────────────────────────

def _session_id(adapter, source) -> str:
    runner = getattr(adapter, "gateway_runner", None)
    try:
        return runner.session_store.peek_session_id(runner._session_key_for_source(source)) or ""
    except Exception:
        return ""


def _authorized(adapter, source) -> bool:
    runner = getattr(adapter, "gateway_runner", None)
    check = getattr(runner, "_is_user_authorized", None)
    if check is None:
        return False
    try:
        return bool(check(source))
    except Exception:
        return False


async def on_reaction(adapter, room_id: str, reacts_to: str, key: str, sender: str) -> bool:
    """A reaction no hermes prompt claimed. True when it was on one of the agent's messages."""
    text = _lookup(room_id, reacts_to)
    if not text:
        return False
    # The reaction addresses the agent as plainly as an @mention would (group rooms ask for one).
    ctx = await adapter._resolve_message_context(room_id, sender, reacts_to, "", {}, {}, mention_claimed=True)
    if ctx is None:
        return True
    display_name, source = ctx[4], ctx[5]
    if not _authorized(adapter, source):
        _log("refused", room=room_id[:12])
        return True
    name = display_name or sender
    emoji = plain(key)
    command = COMMANDS.get(emoji)
    if command:
        event = await adapter._build_inbound_event(
            room_id, sender, f"{reacts_to}:{emoji}", command, {"msgtype": "m.text", "body": command}, {}, ctx=ctx)
        if event is not None:
            event.reply_to_message_id = reacts_to
            event.reply_to_text = text
            event.reply_to_author_id = getattr(adapter, "_user_id", None)
            _log("command", emoji=emoji, room=room_id[:12])
            await adapter.handle_message(event)
        return True
    wrote = write_lesson(name, emoji, text)
    add_note(_session_id(adapter, source), note_text(name, emoji, text), time.time())
    _log("noted", emoji=emoji, lesson=wrote, room=room_id[:12])
    return True


# ── patching the Matrix adapter ──────────────────────────────────────────────

def patch(cls) -> None:
    if getattr(cls, "_logos_reactions", False):
        return
    send_room_message = cls._send_room_message
    edit_message = cls.edit_message
    last_handler = cls._handle_choice_picker_reaction

    async def _send_room_message(self, chat_id, msg_content):
        event_id = await send_room_message(self, chat_id, msg_content)
        await _record(chat_id, event_id, (msg_content or {}).get("body"))
        return event_id

    async def _edit_message(self, chat_id, message_id, content, *args, **kwargs):
        result = await edit_message(self, chat_id, message_id, content, *args, **kwargs)
        if getattr(result, "success", False):
            await _record(chat_id, message_id, content)
        return result

    async def _handle_choice_picker_reaction(self, room_id, reacts_to, key, sender):
        if await last_handler(self, room_id, reacts_to, key, sender):
            return True
        try:
            return await on_reaction(self, room_id, reacts_to, key, sender)
        except Exception as e:
            logger.warning("reactions: %s", e)
            return False

    cls._send_room_message = _send_room_message
    cls.edit_message = _edit_message
    cls._handle_choice_picker_reaction = _handle_choice_picker_reaction
    cls._logos_reactions = True
    logger.info("Matrix: reactions on the agent's messages reach the agent")


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def _patch_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; reactions stay unheard")


def _help(raw_args: str = "", **_) -> str:
    return HELP


def register(ctx):
    ctx.register_hook("pre_llm_call", _note_for_next_turn)
    for name in ("emojis", "reactions"):
        ctx.register_command(name, handler=_help, description="What reacting with an emoji does")
    cls = _find_adapter_class()
    if cls is not None:
        patch(cls)
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-reactions").start()
