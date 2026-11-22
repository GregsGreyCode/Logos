"""logos-reactions: a reaction on an agent's message reaches the agent (Greg, 22 Nov)."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-reactions" / "__init__.py"
ROOM = "!room:logos"
BOT = "@greg-agent:logos"
GREG = "@greg:logos"
REPLY = "The dentist is on Tuesday at 10:30. " + "Bring the forms. " * 20


@pytest.fixture
def rx(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = {}

    async def record_async(chat_id, message_id, text):
        if text:
            store[f"{chat_id}:{message_id}"] = text
    sent = types.SimpleNamespace(record_async=record_async, lookup=lambda c, m: store.get(f"{c}:{m}"), store=store)
    gateway = types.ModuleType("gateway")
    gateway.rich_sent_store = sent
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.rich_sent_store", sent)
    spec = importlib.util.spec_from_file_location("logos_reactions_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.sent = sent
    return mod


class Runner:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.session_store = types.SimpleNamespace(peek_session_id=lambda key: "sess-1" if key == "k:" + ROOM else None)

    def _session_key_for_source(self, source):
        return "k:" + source.chat_id

    def _is_user_authorized(self, source):
        return self.allowed


def adapter_class():
    """Just the parts of hermes's MatrixAdapter that the plugin touches."""
    class MatrixAdapter:
        def __init__(self, runner=None, gate=True):
            self.gateway_runner = runner or Runner()
            self._user_id = BOT
            self.gate = gate
            self.handled = []
            self.prompts = {}
            self.next_id = 0

        async def _send_room_message(self, chat_id, msg_content):
            self.next_id += 1
            return f"$e{self.next_id}"

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            return types.SimpleNamespace(success=True)

        async def _handle_choice_picker_reaction(self, room_id, reacts_to, key, sender):
            return reacts_to in self.prompts

        async def _resolve_message_context(self, room_id, sender, event_id, body, content, relates, mention_claimed=False):
            if not (self.gate or mention_claimed):
                return None
            return body, True, "dm", None, "Greg", types.SimpleNamespace(chat_id=room_id, user_id=sender)

        async def _build_inbound_event(self, room_id, sender, event_id, body, content, relates, ctx=None, **extra):
            return types.SimpleNamespace(text=body, message_id=event_id, source=ctx[5],
                                         reply_to_message_id=None, reply_to_text=None, reply_to_author_id=None)

        async def handle_message(self, event):
            self.handled.append(event)
    return MatrixAdapter


def run(coro):
    return asyncio.run(coro)


def sent_reply(rx, cls, text=REPLY, **kw):
    rx.patch(cls)
    a = cls(**kw)
    event_id = run(a._send_room_message(ROOM, {"msgtype": "m.text", "body": text}))
    return a, event_id


def test_what_the_agent_sends_and_edits_is_remembered(rx):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls, "first words")
    assert rx.sent.lookup(ROOM, eid) == "first words"
    run(a.edit_message(ROOM, eid, "the finished answer", finalize=True))
    assert rx.sent.lookup(ROOM, eid) == "the finished answer"


def test_a_thumbs_up_is_told_at_the_next_turn_and_not_answered(rx):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls)
    assert run(a._handle_choice_picker_reaction(ROOM, eid, "👍🏽", GREG)) is True
    assert a.handled == []
    got = rx._note_for_next_turn(session_id="sess-1")["context"]
    assert got.startswith('Greg reacted 👍 to your message: "The dentist is on Tuesday at 10:30.')
    assert "…" in got and "needs no reply" in got and "lessons.md" not in got
    assert rx._note_for_next_turn(session_id="sess-1") is None     # told once


def test_thumbs_down_and_heart_go_into_lessons(rx, tmp_path):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls)
    run(a._handle_choice_picker_reaction(ROOM, eid, "👎", GREG))
    run(a._handle_choice_picker_reaction(ROOM, eid, "❤", GREG))
    lines = (tmp_path / "brain" / "topics" / "lessons.md").read_text().splitlines()
    assert lines[0] == "# Lessons" and lines[3:4] == [lines[3]]
    lines = lines[4:]
    assert len(lines) == 2
    assert "Greg didn't like this reply (👎 reaction): \"The dentist" in lines[0] and lines[0].startswith("- 20")
    assert "Greg loved this reply (❤️ reaction)" in lines[1]
    note = rx._note_for_next_turn(session_id="sess-1")["context"]
    assert note.count("reacted") == 2 and "noted in topics/lessons.md" in note


def test_question_mark_and_repeat_start_a_turn_as_a_reply(rx):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls)
    run(a._handle_choice_picker_reaction(ROOM, eid, "❓", GREG))
    run(a._handle_choice_picker_reaction(ROOM, eid, "🔁", GREG))
    assert [e.text for e in a.handled] == ["Explain that more.", "Try that again a different way."]
    assert all(e.reply_to_message_id == eid and e.reply_to_text == REPLY and e.reply_to_author_id == BOT
               for e in a.handled)
    assert rx._note_for_next_turn(session_id="sess-1") is None


def test_reactions_on_other_messages_and_hermes_prompts_are_left_alone(rx, tmp_path):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls)
    assert run(a._handle_choice_picker_reaction(ROOM, "$someone-else", "👎", GREG)) is False
    a.prompts[eid] = True                                          # hermes's own picker claims it
    assert run(a._handle_choice_picker_reaction(ROOM, eid, "👎", GREG)) is True
    assert not (tmp_path / "brain").exists() and rx.NOTES == {}


def test_someone_not_allowed_is_not_heard(rx, tmp_path):
    cls = adapter_class()
    a, eid = sent_reply(rx, cls, runner=Runner(allowed=False))
    assert run(a._handle_choice_picker_reaction(ROOM, eid, "❓", "@stranger:x")) is True
    run(a._handle_choice_picker_reaction(ROOM, eid, "👎", "@stranger:x"))
    assert a.handled == [] and rx.NOTES == {} and not (tmp_path / "brain").exists()


def test_notes_are_capped_and_patching_twice_does_nothing(rx):
    for i in range(10):
        rx.add_note("s", f"n{i}", 1000.0 + i)
    assert [n["text"] for n in rx.NOTES["s"]] == [f"n{i}" for i in range(4, 10)]
    cls = adapter_class()
    rx.patch(cls)
    first = cls._send_room_message
    rx.patch(cls)
    assert cls._send_room_message is first


def test_registers_the_hook_and_waits_for_the_adapter(rx, monkeypatch):
    hooks, started = [], []
    monkeypatch.setattr(rx.threading, "Thread", lambda **k: types.SimpleNamespace(start=lambda: started.append(k["name"])))
    for name in [n for n in sys.modules if n.endswith(rx.ADAPTER_MODULE_SUFFIX)]:
        monkeypatch.delitem(sys.modules, name)
    rx.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert hooks == ["pre_llm_call"] and started == ["logos-reactions"]
