"""logos-speaker-chat: a house speaker exchange is posted in the agent's Matrix room (V1.3)."""
import asyncio
import importlib.util
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-speaker-chat" / "__init__.py"
CONF = {"LOGOS_AGENT_NAME": "household", "MATRIX_HOME_ROOM": "!room:example.org"}


def load(monkeypatch, tmp_path, conf=CONF):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k in CONF:
        monkeypatch.delenv(k, raising=False)
    for k, v in conf.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("logos_speaker_chat_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MatrixAdapter:
    def __init__(self, fail_first=False):
        self.sent, self.fail_first = [], fail_first
        self._client = types.SimpleNamespace(crypto=self)
        self.shared = 0

    async def share_keys(self):
        self.shared += 1

    async def _send_room_message(self, room, content):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("no session keys")
        self.sent.append((room, content))
        return "$event"

    def _markdown_to_html(self, text):
        return f"<p>{text}</p>"


class Runner:
    """A gateway runner with its loop on a thread, as hermes's gateway has."""
    def __init__(self, adapter):
        self.adapters = {"matrix": adapter}
        room = CONF["MATRIX_HOME_ROOM"]
        self.session_store = types.SimpleNamespace(list_sessions=lambda: [
            types.SimpleNamespace(session_key=f"agent:main:matrix:group:{room}:$thread", session_id="thread-sid"),
            types.SimpleNamespace(session_key=f"agent:main:api_server:dm:voice-household-sat", session_id="voice-sid"),
            types.SimpleNamespace(session_key=f"agent:main:matrix:group:{room}", session_id="room-sid")])
        self._gateway_loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._gateway_loop.run_forever, daemon=True)
        self.thread.start()

    def drain(self):
        done = threading.Event()
        self._gateway_loop.call_soon_threadsafe(lambda: self._gateway_loop.call_soon(done.set))
        done.wait(2)

    def stop(self):
        self._gateway_loop.call_soon_threadsafe(self._gateway_loop.stop)
        self.thread.join(2)


@pytest.fixture
def mirrored(monkeypatch):
    calls = []
    mirror = types.ModuleType("gateway.mirror")
    mirror.mirror_to_session = lambda *a, **k: calls.append((a, k)) or True
    monkeypatch.setitem(sys.modules, "gateway.mirror", mirror)
    return calls


def test_only_this_agents_speaker_sessions(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert m.is_speaker_session("voice-household-satellite1-20261127", "household")
    assert not m.is_speaker_session("voice-probe-household-20261127-043000", "household")   # the nightly check
    assert not m.is_speaker_session("voice-greg-satellite1-20261127", "household")
    assert not m.is_speaker_session("20261127_101010_abcd", "household")                  # a Matrix chat
    assert not m.is_speaker_session("voice-household-x", "")


def test_content_quotes_the_words_heard_inside_the_agents_message(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    c = m.content_for("  what's on\n tomorrow? ", "**Dentist** at ten.", False, lambda t: "<p><b>Dentist</b> at ten.</p>")
    assert c["msgtype"] == "m.notice"                       # answered out loud: a record, not unread
    assert c["body"] == "🔊 Heard at the house speaker:\n> “what's on tomorrow?”\n\n**Dentist** at ten."
    assert c["org.logos.speaker"] == {"heard": "what's on tomorrow?", "late": False}
    assert "<blockquote>“what&#x27;s on tomorrow?”</blockquote><p><b>Dentist</b>" in c["formatted_body"]
    late = m.content_for("hi", "<b>x</b>", True)
    assert late["msgtype"] == "m.text"                      # the speaker said it would be in the chat
    assert late["org.logos.speaker"]["late"] is True
    assert "<p>&lt;b&gt;x&lt;/b&gt;</p>" in late["formatted_body"]       # no html helper: escaped


def test_a_speaker_turn_is_posted_in_the_home_room_and_the_chat_is_told(monkeypatch, tmp_path, mirrored, caplog):
    m = load(monkeypatch, tmp_path)
    adapter = MatrixAdapter()
    runner = Runner(adapter)
    monkeypatch.setattr(m, "_runner", lambda: runner)
    try:
        with caplog.at_level(logging.INFO, logger="logos.speaker_chat"):
            m.started(session_id="voice-household-sat-20261127", turn_id="t1")
            m.finished(session_id="voice-household-sat-20261127", turn_id="t1",
                       user_message="Add oat milk to the shopping list", assistant_response="Done, oat milk is on it.")
            runner.drain()
            for _ in range(50):
                if mirrored:
                    break
                threading.Event().wait(0.02)
    finally:
        runner.stop()
    (room, content), = adapter.sent
    assert room == "!room:example.org"
    assert content["msgtype"] == "m.notice" and "oat milk" in content["body"]
    (args, kw), = mirrored
    assert args[:2] == ("matrix", "!room:example.org") and kw["role"] == "user"
    assert kw["session_id"] == "room-sid"                    # the room's own conversation, not a thread
    assert "house speaker" in args[2] and "Add oat milk" in args[2]
    assert "oat milk" not in caplog.text.lower()            # never the words


def test_late_answer_is_a_normal_message(monkeypatch, tmp_path, mirrored):
    m = load(monkeypatch, tmp_path)
    adapter = MatrixAdapter(fail_first=True)
    runner = Runner(adapter)
    monkeypatch.setattr(m, "_runner", lambda: runner)
    try:
        m.started(session_id="voice-household-sat-20261127", turn_id="t2")
        m._started["t2"] -= m.SPEAKER_WAIT_S + 1
        m.finished(session_id="voice-household-sat-20261127", turn_id="t2", user_message="plan a week of dinners",
                   assistant_response="Here is the plan…")
        runner.drain()
    finally:
        runner.stop()
    (_, content), = adapter.sent
    assert content["msgtype"] == "m.text" and adapter.shared == 1     # retried once after sharing keys


def test_other_turns_and_empty_answers_are_not_posted(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    posted = []
    monkeypatch.setattr(m, "post", lambda *a, **k: posted.append(a))
    m.finished(session_id="20261127_room_session", turn_id="a", user_message="hi", assistant_response="hello")
    m.finished(session_id="voice-probe-household-20261127-043000", turn_id="b", user_message="What day is it today?",
               assistant_response="Friday.")
    m.finished(session_id="voice-household-sat-20261127", turn_id="c", user_message="hi", assistant_response=None)
    m.finished(session_id="voice-household-sat-20261127", turn_id="d", user_message="", assistant_response="x")
    assert posted == []


def test_without_a_home_room_or_matrix_nothing_breaks(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path, {"LOGOS_AGENT_NAME": "household"})
    runner = Runner(MatrixAdapter())
    assert m.post("hi", "hello", False, runner=runner) is False
    runner.stop()
    m = load(monkeypatch, tmp_path)
    assert m.post("hi", "hello", False, runner=types.SimpleNamespace(adapters={}, _gateway_loop=None)) is False
    monkeypatch.setattr(m, "_runner", lambda: None)
    m.finished(session_id="voice-household-sat-20261127", turn_id="e", user_message="hi", assistant_response="hello")


def test_settings_fall_back_to_the_agents_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("LOGOS_AGENT_NAME=dev\nMATRIX_HOME_ROOM=!dm:example.org\n")
    m = load(monkeypatch, tmp_path, {})
    assert m.settings() == {"LOGOS_AGENT_NAME": "dev", "MATRIX_HOME_ROOM": "!dm:example.org"}


def test_registers_both_hooks(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    got = []
    m.register(types.SimpleNamespace(register_hook=lambda name, fn: got.append(name)))
    assert got == ["pre_llm_call", "post_llm_call"]


def test_no_room_conversation_yet_means_no_mirror(monkeypatch, tmp_path, mirrored):
    m = load(monkeypatch, tmp_path)
    assert m.room_session(types.SimpleNamespace(session_store=types.SimpleNamespace(list_sessions=lambda: [])), "!r") is None
    assert m.room_session(types.SimpleNamespace(), "!r") is None
    m._mirror(types.SimpleNamespace(), "!r", "hi", "hello")
    assert mirrored == []
